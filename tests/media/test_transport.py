from __future__ import annotations

import asyncio
import ipaddress
import os
import threading
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from app.core import resource_budget
from app.core.resource_budget import DiskBudget, InsufficientDiskSpace, media_lease_path
from app.services.media.models import (
    ClipInterval,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
    MediaSource,
    RefreshDescriptor,
)
from app.services.media.transport import (
    DECIMAL_MEDIA_LIMIT,
    CredentialRedirectError,
    DownloadFailed,
    MaterializationError,
    MediaSizeExceeded,
    MediaTransport,
    TransferTimeout,
    UnsafeMediaURL,
    URLPolicy,
    redact_url,
)
from app.tasks import janitor


class Resolver:
    def __init__(self, answers: Mapping[str, Iterable[str]]) -> None:
        self.answers = {host: list(values) for host, values in answers.items()}
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> tuple[str, ...]:
        del port
        self.calls.append(host)
        answers = self.answers.get(host, ["93.184.216.34"])
        value = answers.pop(0) if len(answers) > 1 else answers[0]
        return (value,)


@dataclass
class FakeResponse:
    status_code: int = 200
    headers: dict[str, str] = field(
        default_factory=lambda: {"content-type": "video/mp4"}
    )
    chunks: tuple[bytes, ...] = (b"\x00\x00\x00\x18ftypisompayload",)
    first_delay: float = 0
    header_delay: float = 0
    stall_delay: float = 0
    closed: bool = False
    body_reads: int = 0
    close_error: Exception | None = None

    async def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        del chunk_size
        for index, chunk in enumerate(self.chunks):
            delay = self.first_delay if index == 0 else self.stall_delay
            if delay:
                await asyncio.sleep(delay)
            self.body_reads += 1
            yield chunk

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class DelayedCloseResponse(FakeResponse):
    def __init__(self, *, close_delay: float = 0.02) -> None:
        super().__init__()
        self.close_delay = close_delay
        self.close_started = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await asyncio.sleep(self.close_delay)
        self.closed = True


class CancellationResistantCloseResponse(FakeResponse):
    """A close operation that ignores cancellation until externally released."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.release_close = asyncio.Event()
        self.cancellations = 0

    async def close(self) -> None:
        self.close_started.set()
        while not self.release_close.is_set():
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
        self.closed = True
        self.close_finished.set()


class FakeClient:
    def __init__(self, responses: Iterable[FakeResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.active = 0
        self.max_active = 0

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        resolved_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
        timeout: float,
    ) -> FakeResponse:
        del timeout
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        response = self.responses.pop(0)
        original_close = response.close

        async def close() -> None:
            try:
                await original_close()
            finally:
                self.active -= 1

        response.close = close  # type: ignore[method-assign]
        if response.header_delay:
            try:
                await asyncio.sleep(response.header_delay)
            except asyncio.CancelledError:
                await response.close()
                raise
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "resolved_ip": str(resolved_ip),
            }
        )
        return response


def source_candidate(
    url: str = "https://cdn.example/video.mp4?token=secret",
    *,
    size: int | None = 24,
    headers: tuple[tuple[str, str], ...] = (),
    candidate_id: str = "one",
) -> MediaCandidate:
    return MediaCandidate(
        candidate_id=candidate_id,
        url="https://page.example/watch/1",
        filesize_bytes=size,
        sources=(
            MediaSource(
                format_id="18",
                url=url,
                filesize_bytes=size,
                http_headers=headers,
            ),
        ),
        provider="fixture",
        media_id="1",
        kind=MediaKind.VIDEO,
    )


def request(deadline: float | None = None) -> MediaRequest:
    return MediaRequest(
        canonical_url="https://page.example/watch/1",
        platform="fixture",
        media_id="1",
        deadline=deadline,
    )


def transport(
    tmp_path: Path,
    responses: Iterable[FakeResponse],
    *,
    resolver: Resolver | None = None,
    max_bytes: int = 2_000_000_000,
    first_byte_timeout: float = 0.2,
    stall_timeout: float = 0.02,
    process_runner: object | None = None,
    capacity_bytes: int | None = None,
    transform_poll_interval: float | None = None,
    cleanup_timeout: float | None = None,
) -> tuple[MediaTransport, FakeClient]:
    client = FakeClient(responses)
    policy = URLPolicy(resolver=resolver or Resolver({}))
    options: dict[str, object] = {}
    if transform_poll_interval is not None:
        options["transform_poll_interval"] = transform_poll_interval
    if cleanup_timeout is not None:
        options["cleanup_timeout"] = cleanup_timeout
    return (
        MediaTransport(
            output_dir=tmp_path,
            client=client,
            url_policy=policy,
            disk_budget=DiskBudget(
                tmp_path,
                capacity_bytes=(
                    max_bytes * 3 if capacity_bytes is None else capacity_bytes
                ),
            ),
            max_bytes=max_bytes,
            probe_bytes=32,
            first_byte_timeout=first_byte_timeout,
            stall_timeout=stall_timeout,
            process_runner=process_runner,
            **options,
        ),
        client,
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
    ],
)
@pytest.mark.asyncio
async def test_url_policy_rejects_non_public_addresses(address: str) -> None:
    policy = URLPolicy(resolver=Resolver({"unsafe.example": [address]}))
    with pytest.raises(UnsafeMediaURL):
        await policy.resolve("https://unsafe.example/media")


@pytest.mark.asyncio
async def test_url_policy_rejects_answer_set_containing_one_private_address() -> None:
    async def mixed_answers(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return "93.184.216.34", "10.0.0.1"

    with pytest.raises(UnsafeMediaURL):
        await URLPolicy(resolver=mixed_answers).resolve("https://mixed.example/media")


@pytest.mark.asyncio
async def test_redirect_to_private_ip_is_rejected_before_body_read(
    tmp_path: Path,
) -> None:
    redirect = FakeResponse(
        status_code=302,
        headers={"location": "https://same.example/private"},
        chunks=(b"must-not-read",),
    )
    resolver = Resolver({"same.example": ["93.184.216.34", "127.0.0.1"]})
    media, client = transport(tmp_path, [redirect], resolver=resolver)

    with pytest.raises(UnsafeMediaURL):
        await media.probe("https://same.example/public")

    assert redirect.body_reads == 0
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_nested_media_urls_are_all_validated_before_network_body_read(
    tmp_path: Path,
) -> None:
    response = FakeResponse()
    resolver = Resolver({"nested.example": ["127.0.0.1"]})
    media, client = transport(tmp_path, [response], resolver=resolver)
    candidate = replace(
        source_candidate(),
        items=(MediaItem("nested", MediaKind.PHOTO, "https://nested.example/a.jpg"),),
    )

    with pytest.raises(UnsafeMediaURL):
        await media.materialize(request(), [candidate])

    assert client.requests == []
    assert response.body_reads == 0


@pytest.mark.asyncio
async def test_original_request_url_is_validated_before_candidate_access(
    tmp_path: Path,
) -> None:
    response = FakeResponse()
    resolver = Resolver({"original.example": ["169.254.169.254"]})
    media, client = transport(tmp_path, [response], resolver=resolver)
    original = replace(request(), canonical_url="http://original.example/metadata")

    with pytest.raises(UnsafeMediaURL):
        await media.materialize(original, [source_candidate()])

    assert client.requests == []


@pytest.mark.asyncio
async def test_absolute_deadline_bounds_slow_initial_dns_validation(
    tmp_path: Path,
) -> None:
    async def slow_resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        await asyncio.sleep(0.2)
        return ("93.184.216.34",)

    client = FakeClient([])
    media = MediaTransport(
        output_dir=tmp_path,
        client=client,
        url_policy=URLPolicy(resolver=slow_resolver),
        disk_budget=DiskBudget(tmp_path, capacity_bytes=1_000),
    )
    started = time.monotonic()

    with pytest.raises(TransferTimeout):
        await media.materialize(
            request(), [source_candidate()], deadline=time.monotonic() + 0.01
        )

    assert time.monotonic() - started < 0.08
    assert client.requests == []


@pytest.mark.asyncio
async def test_request_deadline_bounds_slow_initial_dns_validation(
    tmp_path: Path,
) -> None:
    async def slow_resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        await asyncio.sleep(0.2)
        return ("93.184.216.34",)

    client = FakeClient([])
    media = MediaTransport(
        output_dir=tmp_path,
        client=client,
        url_policy=URLPolicy(resolver=slow_resolver),
        disk_budget=DiskBudget(tmp_path, capacity_bytes=1_000),
    )
    started = time.monotonic()

    with pytest.raises(TransferTimeout):
        await media.materialize(
            request(deadline=time.monotonic() + 0.01), [source_candidate()]
        )

    assert time.monotonic() - started < 0.08
    assert client.requests == []


@pytest.mark.asyncio
async def test_deadline_limited_dns_timeout_is_classified_from_its_budget(
    tmp_path: Path,
) -> None:
    async def slow_resolver(host: str, port: int) -> tuple[str, ...]:
        del host, port
        await asyncio.sleep(0.05)
        return ("93.184.216.34",)

    fixed_clock = lambda: 100.0
    media = MediaTransport(
        output_dir=tmp_path,
        client=FakeClient([]),
        url_policy=URLPolicy(resolver=slow_resolver),
        disk_budget=DiskBudget(tmp_path, capacity_bytes=1_000),
        clock=fixed_clock,
    )

    with pytest.raises(TransferTimeout):
        await media.materialize(request(deadline=100.01), [source_candidate()])


@pytest.mark.asyncio
async def test_cross_origin_redirect_strips_credentials_but_keeps_range(
    tmp_path: Path,
) -> None:
    redirect = FakeResponse(
        status_code=303,
        headers={"location": "https://other.example/video.mp4"},
        chunks=(),
    )
    body = FakeResponse()
    media, client = transport(tmp_path, [redirect, body])

    await media.probe(
        "https://cdn.example/video.mp4",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "sid=secret",
            "X-Signed-Token": "also-secret",
        },
    )

    first, second = client.requests
    assert first["headers"]["Authorization"] == "Bearer secret"  # type: ignore[index]
    assert "Authorization" not in second["headers"]  # type: ignore[operator]
    assert "Cookie" not in second["headers"]  # type: ignore[operator]
    assert "X-Signed-Token" not in second["headers"]  # type: ignore[operator]
    assert second["headers"]["Range"] == "bytes=0-31"  # type: ignore[index]


@pytest.mark.asyncio
async def test_body_preserving_cross_origin_request_is_refused(tmp_path: Path) -> None:
    redirect = FakeResponse(
        status_code=307,
        headers={"location": "https://other.example/video.mp4"},
        chunks=(),
    )
    media, client = transport(tmp_path, [redirect])

    with pytest.raises(CredentialRedirectError):
        await media._request(  # verifies the private boundary itself
            "POST",
            "https://cdn.example/video.mp4",
            headers={"Authorization": "Bearer secret"},
            body_sensitive=True,
        )

    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_redirect_budget_is_strict_and_never_reads_redirect_bodies(
    tmp_path: Path,
) -> None:
    first = FakeResponse(status_code=302, headers={"location": "/two"})
    second = FakeResponse(status_code=302, headers={"location": "/three"})
    media, client = transport(tmp_path, [first, second])
    media.max_redirects = 1

    with pytest.raises(DownloadFailed):
        await media.probe("https://cdn.example/one")

    assert len(client.requests) == 2
    assert first.body_reads == second.body_reads == 0


@pytest.mark.asyncio
async def test_probe_uses_bounded_range_and_never_reads_the_whole_body(
    tmp_path: Path,
) -> None:
    response = FakeResponse(chunks=(b"\x00\x00\x00\x18ftyp", b"x" * 1000))
    media, client = transport(tmp_path, [response])

    signature = await media.probe("https://cdn.example/video.mp4")

    assert signature.startswith(b"\x00\x00\x00\x18ftyp")
    assert client.requests[0]["headers"]["Range"] == "bytes=0-31"  # type: ignore[index]
    assert len(signature) <= 32
    assert response.closed


@pytest.mark.asyncio
async def test_probe_rejects_declared_media_with_non_media_signature(
    tmp_path: Path,
) -> None:
    media, _ = transport(tmp_path, [FakeResponse(chunks=(b"not really media",))])

    with pytest.raises(DownloadFailed, match="signature"):
        await media.probe("https://cdn.example/video.mp4")


@pytest.mark.asyncio
async def test_declared_size_over_decimal_limit_is_rejected_before_open(
    tmp_path: Path,
) -> None:
    media, client = transport(tmp_path, [], max_bytes=100)
    candidate = source_candidate(size=101)

    with pytest.raises(MediaSizeExceeded):
        await media.materialize(request(), [candidate])

    assert client.requests == []
    assert DECIMAL_MEDIA_LIMIT == 2_000_000_000


@pytest.mark.asyncio
async def test_first_byte_budget_spans_headers_and_first_body_byte(
    tmp_path: Path,
) -> None:
    response = FakeResponse(header_delay=0.08, first_delay=0.09)
    media, _ = transport(tmp_path, [response], first_byte_timeout=0.15)
    started = time.monotonic()

    with pytest.raises(TransferTimeout):
        await media.probe("https://cdn.example/video.mp4")

    assert time.monotonic() - started < 0.23
    assert response.closed


@pytest.mark.asyncio
async def test_expired_work_deadline_still_allows_bounded_response_cleanup(
    tmp_path: Path,
) -> None:
    response = DelayedCloseResponse()
    media, _ = transport(tmp_path, [])

    await media._close_response(response, time.monotonic() - 1)

    assert response.closed


@pytest.mark.asyncio
async def test_cancellation_waits_for_response_cleanup_then_propagates(
    tmp_path: Path,
) -> None:
    response = DelayedCloseResponse()
    media, _ = transport(tmp_path, [])
    cleanup = asyncio.create_task(
        media._close_response(response, time.monotonic() + 10)
    )
    await response.close_started.wait()

    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert response.closed


@pytest.mark.asyncio
async def test_cancellation_resistant_response_cleanup_has_hard_bound(
    tmp_path: Path,
) -> None:
    response = CancellationResistantCloseResponse()
    media, _ = transport(tmp_path, [], cleanup_timeout=0.02)
    cleanup = asyncio.create_task(
        media._close_response(response, time.monotonic() + 10)
    )
    await response.close_started.wait()
    release_handle = asyncio.get_running_loop().call_later(
        0.2, response.release_close.set
    )
    started = time.monotonic()

    try:
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        elapsed = time.monotonic() - started
        assert elapsed < 0.1
    finally:
        response.release_close.set()
        release_handle.cancel()
        await asyncio.wait_for(response.close_finished.wait(), timeout=0.3)

    assert response.cancellations == 1
    assert response.closed


@pytest.mark.asyncio
async def test_stream_aborts_at_limit_without_buffering_and_removes_partial(
    tmp_path: Path,
) -> None:
    probe = FakeResponse(chunks=(b"\x00\x00\x00\x18ftyp",))
    body = FakeResponse(chunks=(b"a" * 40, b"b" * 40, b"c" * 40))
    media, _ = transport(tmp_path, [probe, body], max_bytes=100)
    candidate = source_candidate(size=None)

    with pytest.raises(MediaSizeExceeded):
        await media.materialize(request(), [candidate])

    assert body.body_reads == 3
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_first_byte_timeout_fails_over_and_cleans_partial(tmp_path: Path) -> None:
    first_probe = FakeResponse()
    first_body = FakeResponse(first_delay=0.2)
    second_probe = FakeResponse()
    second_body = FakeResponse(chunks=(b"\x00\x00\x00\x18ftypwinner",))
    media, _ = transport(
        tmp_path,
        [first_probe, first_body, second_probe, second_body],
        first_byte_timeout=0.05,
    )
    first = source_candidate(candidate_id="slow", size=30_000_000)
    second = source_candidate(
        "https://other.example/winner.mp4", candidate_id="winner", size=30_000_000
    )

    item = await media.materialize(request(), [first, second])

    assert item.candidate.candidate_id == "winner"
    assert item.paths[0].read_bytes().endswith(b"winner")
    assert not list(tmp_path.glob("*.part"))
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_stall_timeout_fails_over(tmp_path: Path) -> None:
    first_probe = FakeResponse()
    first_body = FakeResponse(chunks=(b"first", b"stalled"), stall_delay=0.1)
    second_probe = FakeResponse()
    second_body = FakeResponse(chunks=(b"\x00\x00\x00\x18ftypwinner",))
    media, _ = transport(
        tmp_path,
        [first_probe, first_body, second_probe, second_body],
        first_byte_timeout=0.1,
        stall_timeout=0.02,
    )

    item = await media.materialize(
        request(),
        [
            source_candidate(candidate_id="stalled", size=30_000_000),
            source_candidate(
                "https://other.example/winner.mp4",
                candidate_id="winner",
                size=30_000_000,
            ),
        ],
    )

    assert item.candidate.candidate_id == "winner"
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_two_small_candidates_may_download_concurrently(tmp_path: Path) -> None:
    responses = [
        FakeResponse(first_delay=0.01),
        FakeResponse(first_delay=0.01),
        FakeResponse(first_delay=0.01),
        FakeResponse(first_delay=0.01),
    ]
    media, client = transport(tmp_path, responses, first_byte_timeout=0.1)
    first = source_candidate(candidate_id="a", size=20_000_000)
    second = source_candidate(
        "https://other.example/b.mp4", candidate_id="b", size=20_000_000
    )

    item = await media.materialize(request(), [first, second])

    assert client.max_active == 2
    assert len(list(tmp_path.glob("media_*"))) >= 2
    await item.release(delete=True)
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.asyncio
async def test_cancellation_closes_stream_and_removes_partial_and_lease(
    tmp_path: Path,
) -> None:
    probe = FakeResponse()
    body = FakeResponse(first_delay=1)
    media, _ = transport(tmp_path, [probe, body], first_byte_timeout=2, stall_timeout=2)

    task = asyncio.create_task(
        media.materialize(request(), [source_candidate(size=30_000_000)])
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body.closed
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_stream_cancellation_is_not_masked_by_cleanup_failure(
    tmp_path: Path,
) -> None:
    probe = FakeResponse()
    body = FakeResponse(first_delay=1, close_error=OSError("close failed"))
    media, _ = transport(tmp_path, [probe, body], first_byte_timeout=2)
    task = asyncio.create_task(
        media.materialize(request(), [source_candidate(size=30_000_000)])
    )
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert body.closed
    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


class RecordingProcessRunner:
    def __init__(
        self,
        *,
        output: bytes = b"\x00\x00\x00\x18ftypisomfinal",
        return_code: int = 0,
        block: bool = False,
    ) -> None:
        self.output = output
        self.return_code = return_code
        self.block = block
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, command: list[str], timeout: float) -> int:
        self.commands.append(tuple(command))
        self.timeouts.append(timeout)
        output = Path(command[-1])
        output.write_bytes(self.output)
        self.started.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self.return_code


@pytest.mark.asyncio
async def test_adopt_local_owns_and_clips_derived_artifact(tmp_path: Path) -> None:
    runner = RecordingProcessRunner()
    media, _ = transport(tmp_path, [], process_runner=runner)
    source = tmp_path / "slideshow-source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
    candidate = replace(
        source_candidate(size=source.stat().st_size),
        candidate_id="slideshow-video",
        url="https://www.tiktok.com/@tester/photo/123",
        container="mp4",
        media_id="123",
        kind=MediaKind.VIDEO,
        items=(
            MediaItem(
                "123",
                MediaKind.VIDEO,
                "https://www.tiktok.com/@tester/photo/123",
                container="mp4",
            ),
        ),
    )
    clipped = MediaRequest(
        canonical_url="https://www.tiktok.com/@tester/photo/123",
        platform="tiktok",
        media_id="123",
        kind=MediaKind.VIDEO,
        clip=ClipInterval(10, 20),
        output_variant="slideshow-video",
    )

    item = await media.adopt_local(clipped, source, candidate)

    assert not source.exists()
    assert len(item.paths) == 1
    assert item.paths[0].is_file()
    assert media_lease_path(item.paths[0]).is_file()
    command = runner.commands[0]
    assert command[command.index("-ss") + 1] == "10"
    assert command[command.index("-t") + 1] == "10"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"

    await item.release(delete=True)

    assert not item.paths[0].exists()
    assert not media_lease_path(item.paths[0]).exists()


class CancellationResistantProcessRunner:
    """An injected runner that ignores cancellation until externally released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0

    async def __call__(self, command: list[str], timeout: float) -> int:
        del timeout
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftyp")
        self.started.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
        self.finished.set()
        return 1


def _append_bytes(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)


class GrowingProcessRunner:
    def __init__(self, *, chunk: bytes = b"12345678") -> None:
        self.chunk = chunk
        self.bytes_written = 0
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self, command: list[str], timeout: float) -> int:
        del timeout
        output = Path(command[-1])
        output.write_bytes(b"\x00\x00\x00\x18ftyp")
        self.bytes_written = output.stat().st_size
        self.started.set()
        try:
            while True:
                await asyncio.to_thread(_append_bytes, output, self.chunk)
                self.bytes_written += len(self.chunk)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def split_candidate() -> MediaCandidate:
    return MediaCandidate(
        candidate_id="137+140",
        url="https://page.example/watch/1",
        filesize_bytes=48,
        sources=(
            MediaSource(
                "137",
                "https://video.example/v.mp4",
                video_codec="avc1",
                container="mp4",
                filesize_bytes=24,
            ),
            MediaSource(
                "140",
                "https://audio.example/a.m4a",
                audio_codec="mp4a",
                container="m4a",
                filesize_bytes=24,
            ),
        ),
        mux_mode="copy",
        container="mp4",
        provider="fixture",
        media_id="1",
        kind=MediaKind.VIDEO,
    )


@pytest.mark.asyncio
async def test_clipped_split_video_audio_is_reencoded_to_one_exact_deliverable(
    tmp_path: Path,
) -> None:
    responses = [
        FakeResponse(),
        FakeResponse(),
        FakeResponse(chunks=(b"ID3audio",), headers={"content-type": "audio/mp4"}),
        FakeResponse(chunks=(b"ID3audio",), headers={"content-type": "audio/mp4"}),
    ]
    runner = RecordingProcessRunner()
    media, _ = transport(tmp_path, responses, process_runner=runner)
    clipped = replace(request(), clip=ClipInterval(10, 20))

    item = await media.materialize(clipped, [split_candidate()])

    assert len(item.paths) == 1
    assert item.paths[0].suffix == ".mp4"
    command = runner.commands[0]
    first_input = command.index("-i")
    second_input = command.index("-i", first_input + 1)
    assert second_input < command.index("-ss") < command.index("-t")
    assert command[command.index("-ss") + 1] == "10"
    assert command[command.index("-t") + 1] == "10"
    assert "copy" not in command
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command.index("-t") < command.index("-c:v")
    assert not any(
        path.name.endswith((".m4a", ".mp4.part")) for path in tmp_path.iterdir()
    )
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_unclipped_split_video_audio_retains_copy_mux(
    tmp_path: Path,
) -> None:
    responses = [FakeResponse(), FakeResponse(), FakeResponse(), FakeResponse()]
    runner = RecordingProcessRunner()
    media, _ = transport(tmp_path, responses, process_runner=runner)

    item = await media.materialize(request(), [split_candidate()])

    command = runner.commands[0]
    assert command[command.index("-c") + 1] == "copy"
    assert "-ss" not in command
    assert "-t" not in command
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_extract_mp3_uses_ffmpeg_and_returns_only_mp3(tmp_path: Path) -> None:
    responses = [
        FakeResponse(chunks=(b"ID3source",), headers={"content-type": "audio/mp4"}),
        FakeResponse(chunks=(b"ID3source",), headers={"content-type": "audio/mp4"}),
    ]
    runner = RecordingProcessRunner(output=b"ID3final")
    media, _ = transport(tmp_path, responses, process_runner=runner)
    candidate = replace(
        source_candidate(
            "https://audio.example/source.m4a", size=24, candidate_id="140"
        ),
        has_video=False,
        mux_mode="extract-mp3",
        container="mp3",
        kind=MediaKind.AUDIO,
    )

    clipped = replace(
        request(),
        kind=MediaKind.AUDIO,
        audio_format="mp3",
        clip=ClipInterval(2.5, 8),
    )
    item = await media.materialize(clipped, [candidate])

    assert len(item.paths) == 1
    assert item.paths[0].suffix == ".mp3"
    command = runner.commands[0]
    assert command[command.index("-c:a") + 1] == "libmp3lame"
    assert "-vn" in command
    assert command[command.index("-f") + 1] == "mp3"
    assert command.index("-i") < command.index("-ss") < command.index("-t")
    assert command.index("-t") < command.index("-c:a")
    assert command[command.index("-ss") + 1] == "2.5"
    assert command[command.index("-t") + 1] == "5.5"
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_animation_transform_mutes_and_caps_mp4_duration(tmp_path: Path) -> None:
    responses = [FakeResponse(), FakeResponse()]
    runner = RecordingProcessRunner()
    media, _ = transport(tmp_path, responses, process_runner=runner)
    candidate = replace(
        source_candidate(size=24),
        has_audio=False,
        mux_mode="mute-mp4",
        container="mp4",
        kind=MediaKind.ANIMATION,
    )
    animation = replace(request(), kind=MediaKind.ANIMATION)

    item = await media.materialize(animation, [candidate])

    command = runner.commands[0]
    assert item.paths[0].suffix == ".mp4"
    assert "-an" in command
    assert command[command.index("-t") + 1] == "60"
    assert command[command.index("-c:v") + 1] == "copy"
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_transform_output_is_stopped_while_crossing_byte_cap(
    tmp_path: Path,
) -> None:
    responses = [FakeResponse(), FakeResponse()]
    runner = GrowingProcessRunner()
    media, _ = transport(
        tmp_path,
        responses,
        max_bytes=48,
        process_runner=runner,
        transform_poll_interval=0.001,
    )
    candidate = replace(source_candidate(size=24), mux_mode="copy", container="mp4")

    with pytest.raises(MediaSizeExceeded):
        await media.materialize(request(), [candidate])

    assert runner.cancelled
    assert runner.bytes_written <= 48 + len(runner.chunk)
    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_transform_reserves_input_plus_full_output_cap_before_process_start(
    tmp_path: Path,
) -> None:
    responses = [FakeResponse(), FakeResponse()]
    runner = RecordingProcessRunner()
    media, _ = transport(
        tmp_path,
        responses,
        max_bytes=100,
        capacity_bytes=100,
        process_runner=runner,
    )
    candidate = replace(source_candidate(size=24), mux_mode="copy", container="mp4")

    with pytest.raises(InsufficientDiskSpace):
        await media.materialize(request(), [candidate])

    assert runner.commands == []
    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_transform_cancellation_cleans_inputs_output_and_lease(
    tmp_path: Path,
) -> None:
    responses = [FakeResponse(), FakeResponse(), FakeResponse(), FakeResponse()]
    runner = RecordingProcessRunner(block=True)
    media, _ = transport(tmp_path, responses, process_runner=runner)
    task = asyncio.create_task(media.materialize(request(), [split_candidate()]))
    await runner.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.cancelled
    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_cancellation_resistant_process_cleanup_has_hard_bound(
    tmp_path: Path,
) -> None:
    runner = CancellationResistantProcessRunner()
    media, _ = transport(
        tmp_path,
        [],
        process_runner=runner,
        cleanup_timeout=0.02,
    )
    partial = tmp_path / "transform.mp4.part"
    transform = asyncio.create_task(
        media._run_transform(["ffmpeg", str(partial)], partial, time.monotonic() + 10)
    )
    await runner.started.wait()
    release_handle = asyncio.get_running_loop().call_later(0.2, runner.release.set)
    started = time.monotonic()

    try:
        transform.cancel()
        with pytest.raises(asyncio.CancelledError):
            await transform
        elapsed = time.monotonic() - started
        assert elapsed < 0.1
    finally:
        runner.release.set()
        release_handle.cancel()
        await asyncio.wait_for(runner.finished.wait(), timeout=0.3)
        partial.unlink(missing_ok=True)

    assert runner.cancellations == 1


@pytest.mark.asyncio
async def test_transform_failure_cleans_inputs_output_and_lease(tmp_path: Path) -> None:
    responses = [FakeResponse(), FakeResponse(), FakeResponse(), FakeResponse()]
    runner = RecordingProcessRunner(return_code=1)
    media, _ = transport(tmp_path, responses, process_runner=runner)

    with pytest.raises(MaterializationError):
        await media.materialize(request(), [split_candidate()])

    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.asyncio
async def test_close_failure_after_promotion_removes_final_and_lease(
    tmp_path: Path,
) -> None:
    probe = FakeResponse()
    body = FakeResponse(close_error=OSError("close failed"))
    media, _ = transport(tmp_path, [probe, body])

    with pytest.raises(MaterializationError):
        await media.materialize(request(), [source_candidate(size=24)])

    assert not list(tmp_path.glob("media_*"))
    assert not list(tmp_path.glob("*.lease"))


@pytest.mark.parametrize("transform_output", [False, True])
@pytest.mark.asyncio
async def test_file_promotion_holds_destination_lock_against_janitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transform_output: bool,
) -> None:
    observations: list[tuple[int, bool, bool]] = []
    deletion_results: dict[Path, int] = {}
    recorded: set[Path] = set()
    real_replace = os.replace

    def record(final_path: Path) -> None:
        if final_path in deletion_results and final_path not in recorded:
            observations.append(
                (
                    deletion_results[final_path],
                    final_path.exists(),
                    media_lease_path(final_path).exists(),
                )
            )
            recorded.add(final_path)

    def racing_replace(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        real_replace(source, destination)
        if (
            destination_path.name.startswith("media_")
            and destination_path.name.endswith(".lease")
            and not destination_path.name.endswith(".part.lease")
        ):
            record(Path(str(destination_path).removesuffix(".lease")))
        if (
            source_path.name.startswith("media_")
            and source_path.name.endswith(".part")
            and destination_path == source_path.with_suffix("")
        ):
            deleted: list[int] = []

            def purge() -> None:
                deleted.append(janitor._aggressive_purge_temp(str(tmp_path)))

            purge_thread = threading.Thread(target=purge)
            purge_thread.start()
            purge_thread.join(timeout=1)
            assert not purge_thread.is_alive()
            deletion_results[destination_path] = deleted[0]
            if media_lease_path(destination_path).exists():
                record(destination_path)

    monkeypatch.setattr(resource_budget.os, "replace", racing_replace)
    runner = RecordingProcessRunner() if transform_output else None
    media, _ = transport(
        tmp_path,
        [FakeResponse(), FakeResponse()],
        process_runner=runner,
    )
    candidate = source_candidate(size=24)
    if transform_output:
        candidate = replace(candidate, mux_mode="copy", container="mp4")

    item = None
    caught: FileNotFoundError | None = None
    try:
        item = await media.materialize(request(), [candidate])
    except FileNotFoundError as error:
        caught = error

    expected_promotions = 2 if transform_output else 1
    assert observations == [(0, True, True)] * expected_promotions
    assert caught is None
    assert item is not None
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_large_candidates_transfer_only_once_at_a_time(tmp_path: Path) -> None:
    responses = [FakeResponse(), FakeResponse(), FakeResponse(), FakeResponse()]
    media, client = transport(tmp_path, responses)
    first = source_candidate(candidate_id="a", size=20_000_001)
    second = source_candidate(
        "https://other.example/b.mp4", candidate_id="b", size=20_000_001
    )

    item = await media.materialize(request(), [first, second])

    assert client.max_active == 1
    await item.release(delete=True)


class Refresher:
    def __init__(self, fresh: MediaCandidate) -> None:
        self.fresh = fresh
        self.calls = 0

    async def refresh(
        self,
        request: MediaRequest,
        descriptor: RefreshDescriptor,
        *,
        attempt: int,
        deadline: float,
    ) -> MediaCandidate:
        del request, deadline
        assert attempt == 0
        self.calls += 1
        return replace(self.fresh, refresh=descriptor)


class RaisingRefresher:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, *args: object, **kwargs: object) -> MediaCandidate:
        del args, kwargs
        self.calls += 1
        raise RuntimeError("provider leaked its internal failure")


@pytest.mark.asyncio
async def test_expired_source_is_refreshed_once_without_persisting_signed_url(
    tmp_path: Path,
) -> None:
    denied = FakeResponse(status_code=403, chunks=())
    fresh_probe = FakeResponse()
    fresh_body = FakeResponse()
    media, _ = transport(tmp_path, [denied, fresh_probe, fresh_body])
    stale = replace(
        source_candidate(),
        refresh=RefreshDescriptor("fixture", "1", "one"),
    )
    fresh = source_candidate("https://fresh.example/video.mp4?sig=new")
    refresher = Refresher(fresh)

    item = await media.materialize(
        request(), [stale], refreshers={"fixture": refresher}
    )

    assert refresher.calls == 1
    assert "sig=" not in repr(item)
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_explicit_source_expiry_refreshes_before_stale_url_is_opened(
    tmp_path: Path,
) -> None:
    fresh_probe = FakeResponse()
    fresh_body = FakeResponse()
    media, client = transport(tmp_path, [fresh_probe, fresh_body])
    stale_source = replace(source_candidate().sources[0], expires_at=time.time() - 1)
    stale = replace(
        source_candidate(),
        sources=(stale_source,),
        refresh=RefreshDescriptor("fixture", "1", "one"),
    )
    fresh = source_candidate("https://fresh.example/video.mp4?sig=new")
    refresher = Refresher(fresh)

    item = await media.materialize(
        request(), [stale], refreshers={"fixture": refresher}
    )

    assert refresher.calls == 1
    assert all("token=secret" not in str(call["url"]) for call in client.requests)
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_refresh_exception_is_normalized_and_next_candidate_wins(
    tmp_path: Path,
) -> None:
    media, _ = transport(
        tmp_path,
        [FakeResponse(status_code=403), FakeResponse(), FakeResponse()],
    )
    first = replace(
        source_candidate(size=30_000_000),
        refresh=RefreshDescriptor("fixture", "1", "one"),
    )
    second = source_candidate(
        "https://other.example/winner.mp4",
        size=30_000_000,
        candidate_id="winner",
    )
    refresher = RaisingRefresher()

    item = await media.materialize(
        request(), [first, second], refreshers={"fixture": refresher}
    )

    assert item.candidate.candidate_id == "winner"
    assert refresher.calls == 1
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_only_one_refresh_is_allowed_across_all_candidate_descriptors(
    tmp_path: Path,
) -> None:
    media, _ = transport(
        tmp_path,
        [
            FakeResponse(status_code=403),
            FakeResponse(status_code=403),
            FakeResponse(status_code=403),
        ],
    )
    first = replace(
        source_candidate(size=30_000_000),
        refresh=RefreshDescriptor("fixture", "1", "one"),
    )
    second = replace(
        source_candidate(
            "https://other.example/stale.mp4",
            size=30_000_000,
            candidate_id="two",
        ),
        provider="other",
        refresh=RefreshDescriptor("other", "1", "two"),
    )
    first_refresher = Refresher(
        source_candidate(
            "https://fresh.example/still-denied.mp4",
            size=30_000_000,
        )
    )
    second_refresher = Refresher(
        replace(
            source_candidate(
                "https://fresh.example/other.mp4",
                size=30_000_000,
                candidate_id="two",
            ),
            provider="other",
        )
    )

    with pytest.raises(MaterializationError):
        await media.materialize(
            request(),
            [first, second],
            refreshers={"fixture": first_refresher, "other": second_refresher},
        )

    assert first_refresher.calls == 1
    assert second_refresher.calls == 0


@pytest.mark.asyncio
async def test_mismatched_refresh_identity_is_rejected_then_fails_over(
    tmp_path: Path,
) -> None:
    media, _ = transport(
        tmp_path,
        [FakeResponse(status_code=403), FakeResponse(), FakeResponse()],
    )
    stale = replace(
        source_candidate(size=30_000_000),
        refresh=RefreshDescriptor("fixture", "1", "one"),
    )
    mismatch = replace(
        source_candidate(
            "https://fresh.example/wrong.mp4",
            size=30_000_000,
            candidate_id="wrong-variant",
        ),
        auth_scope="private",
    )
    fallback = source_candidate(
        "https://other.example/winner.mp4",
        size=30_000_000,
        candidate_id="winner",
    )

    item = await media.materialize(
        request(), [stale, fallback], refreshers={"fixture": Refresher(mismatch)}
    )

    assert item.candidate.candidate_id == "winner"
    await item.release(delete=True)


@pytest.mark.asyncio
async def test_refresh_descriptor_must_match_request_and_original_candidate(
    tmp_path: Path,
) -> None:
    media, _ = transport(
        tmp_path,
        [
            FakeResponse(status_code=403),
            FakeResponse(),
            FakeResponse(),
            FakeResponse(),
            FakeResponse(),
        ],
    )
    stale = replace(
        source_candidate(size=30_000_000),
        refresh=RefreshDescriptor("fixture", "different-media", "one"),
    )
    wrong_identity = replace(
        source_candidate(
            "https://fresh.example/wrong-media.mp4",
            size=30_000_000,
        ),
        media_id="different-media",
    )
    fallback = source_candidate(
        "https://other.example/winner.mp4",
        size=30_000_000,
        candidate_id="winner",
    )

    item = await media.materialize(
        request(),
        [stale, fallback],
        refreshers={"fixture": Refresher(wrong_identity)},
    )

    assert item.candidate.candidate_id == "winner"
    await item.release(delete=True)


def test_url_redaction_removes_userinfo_query_and_fragment() -> None:
    assert (
        redact_url("https://user:pass@cdn.example/a.mp4?token=secret#part")
        == "https://cdn.example/a.mp4"
    )


@pytest.mark.asyncio
async def test_both_failed_candidates_raise_redacted_error(tmp_path: Path) -> None:
    media, _ = transport(
        tmp_path,
        [FakeResponse(status_code=500), FakeResponse(status_code=500)],
    )

    with pytest.raises(MaterializationError) as captured:
        await media.materialize(
            request(),
            [
                source_candidate(),
                source_candidate(
                    "https://other.example/b.mp4?secret=two", candidate_id="b"
                ),
            ],
        )

    assert "secret" not in str(captured.value)
