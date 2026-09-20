from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from app.core.resource_budget import DiskBudget
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
    first_byte_timeout: float = 0.02,
    stall_timeout: float = 0.02,
    process_runner: object | None = None,
) -> tuple[MediaTransport, FakeClient]:
    client = FakeClient(responses)
    policy = URLPolicy(resolver=resolver or Resolver({}))
    return (
        MediaTransport(
            output_dir=tmp_path,
            client=client,
            url_policy=policy,
            disk_budget=DiskBudget(tmp_path, capacity_bytes=max_bytes * 3),
            max_bytes=max_bytes,
            probe_bytes=32,
            first_byte_timeout=first_byte_timeout,
            stall_timeout=stall_timeout,
            process_runner=process_runner,
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
async def test_split_video_audio_is_copy_muxed_to_one_clipped_deliverable(
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
    assert command[command.index("-c") + 1] == "copy"
    assert not any(
        path.name.endswith((".m4a", ".mp4.part")) for path in tmp_path.iterdir()
    )
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

    item = await media.materialize(
        replace(request(), kind=MediaKind.AUDIO, audio_format="mp3"), [candidate]
    )

    assert len(item.paths) == 1
    assert item.paths[0].suffix == ".mp3"
    command = runner.commands[0]
    assert command[command.index("-c:a") + 1] == "libmp3lame"
    assert "-vn" in command
    assert command[command.index("-f") + 1] == "mp3"
    await item.release(delete=True)


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
