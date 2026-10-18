from __future__ import annotations

import asyncio
import builtins
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.services import instagram, pinterest
from app.services.cobalt import CobaltPickerItem, CobaltResult, CobaltService


class StreamResponse:
    status_code = 200

    def __init__(
        self,
        chunks: list[bytes],
        *,
        length: str | None = None,
        block_after_body: bool = False,
    ) -> None:
        self.chunks = chunks
        self.headers = {"Content-Length": length} if length is not None else {}
        self.block_after_body = block_after_body
        self.body_consumed = asyncio.Event()

    async def aiter_content(self):
        for chunk in self.chunks:
            yield chunk
        self.body_consumed.set()
        if self.block_after_body:
            await asyncio.Event().wait()


class StreamSession:
    def __init__(self, response: StreamResponse) -> None:
        self.response = response
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def get(self, *args, **kwargs):
        return self.response


@pytest.fixture()
def configure_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sessions: list[StreamSession] = []

    def configure(kind: str, response: StreamResponse) -> None:
        module = pinterest if kind == "pinterest" else instagram
        monkeypatch.setattr(module, "TEMP_DIR", str(tmp_path))
        monkeypatch.setattr("app.core.config.TEMP_DIR", str(tmp_path))
        monkeypatch.setattr(module, "MAX_MEDIA_BYTES", 4, raising=False)

        def session_factory(*args, **kwargs):
            session = StreamSession(response)
            sessions.append(session)
            return session

        monkeypatch.setattr(module, "AsyncSession", session_factory)
        monkeypatch.setattr("curl_cffi.requests.AsyncSession", session_factory)
        monkeypatch.setattr(instagram.InstagramService, "_init_pool", lambda: None)
        monkeypatch.setattr(
            instagram.InstagramService,
            "_get_available_session",
            AsyncMock(return_value=(-1, None)),
        )
        monkeypatch.setattr(
            CobaltService,
            "process",
            AsyncMock(
                return_value=CobaltResult(
                    status="redirect", url="https://cdn.example/media.mp4"
                )
            ),
        )

    return configure, sessions


async def download(kind: str):
    if kind == "pinterest":
        return await pinterest._stream_download(
            "https://cdn.example/media.mp4", "pin_", "mp4"
        )
    if kind == "post":
        path, _ = await instagram.InstagramService.download_post(
            "https://www.instagram.com/p/ABC/"
        )
        return path
    item = instagram.IGStoryItem(
        mediaid="123",
        is_video=True,
        url="https://cdn.example/media.mp4",
        thumbnail_url="",
        timestamp=datetime(2026, 10, 18, tzinfo=UTC),
    )
    path, _ = await instagram.InstagramService.download_story_item(item)
    return path


@pytest.mark.parametrize("kind", ["story", "post", "pinterest"])
async def test_cancelled_legacy_cdn_download_removes_its_partial_file(
    configure_stream, tmp_path: Path, kind: str
) -> None:
    configure, sessions = configure_stream
    response = StreamResponse([b"abc"], block_after_body=True)
    configure(kind, response)
    task = asyncio.create_task(download(kind))
    await asyncio.wait_for(response.body_consumed.wait(), timeout=2)
    assert list(tmp_path.iterdir())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(tmp_path.iterdir()) == []
    assert sessions and all(session.closed for session in sessions)


@pytest.mark.parametrize("kind", ["story", "post", "pinterest"])
@pytest.mark.parametrize(
    ("chunks", "length"),
    [([], None), ([b"abc"], "4"), ([b"abc"], "100"), ([b"abc", b"de"], None)],
    ids=["empty", "truncated", "declared-over-cap", "streamed-over-cap"],
)
async def test_legacy_cdn_download_rejects_empty_truncated_or_oversize_media(
    configure_stream, tmp_path: Path, kind: str, chunks: list[bytes], length: str | None
) -> None:
    configure, sessions = configure_stream
    configure(kind, StreamResponse(chunks, length=length))

    assert await download(kind) is None
    assert list(tmp_path.iterdir()) == []
    assert sessions and all(session.closed for session in sessions)


@pytest.mark.parametrize("kind", ["story", "post", "pinterest"])
async def test_legacy_cdn_download_preserves_complete_media_at_the_byte_cap(
    configure_stream, tmp_path: Path, kind: str
) -> None:
    configure, sessions = configure_stream
    configure(kind, StreamResponse([b"ab", b"cd"], length="4"))

    path = await download(kind)

    assert path is not None
    assert Path(path).read_bytes() == b"abcd"
    assert sessions and all(session.closed for session in sessions)


@pytest.mark.parametrize("kind", ["story", "post", "pinterest"])
@pytest.mark.parametrize("encoding", ["gzip", "br"])
async def test_encoded_cdn_response_keeps_decoded_byte_cap_without_wire_length_comparison(
    configure_stream, kind: str, encoding: str
) -> None:
    configure, _ = configure_stream
    response = StreamResponse([b"ab", b"cd"], length="24")
    response.headers["Content-Encoding"] = encoding
    configure(kind, response)

    path = await download(kind)

    assert path is not None
    assert Path(path).read_bytes() == b"abcd"


@pytest.mark.parametrize("kind", ["story", "post", "pinterest"])
async def test_encoded_cdn_response_still_rejects_decoded_bytes_over_the_cap(
    configure_stream, tmp_path: Path, kind: str
) -> None:
    configure, _ = configure_stream
    response = StreamResponse([b"abcde"], length="1")
    response.headers["Content-Encoding"] = "gzip"
    configure(kind, response)

    assert await download(kind) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("outcome", ["cancelled", "missing", "exception"])
async def test_pinterest_carousel_cleans_all_completed_siblings_on_incomplete_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    first = tmp_path / "first.jpg"
    ready = asyncio.Event()
    monkeypatch.setattr(
        CobaltService,
        "process",
        AsyncMock(
            return_value=CobaltResult(
                status="picker",
                picker=[
                    CobaltPickerItem(type="photo", url="https://cdn.example/first.jpg"),
                    CobaltPickerItem(
                        type="photo", url="https://cdn.example/second.jpg"
                    ),
                ],
            )
        ),
    )

    async def download_item(url: str, prefix: str, ext: str):
        if url.endswith("first.jpg"):
            first.write_bytes(b"photo")
            ready.set()
            return str(first)
        await ready.wait()
        if outcome == "cancelled":
            await asyncio.Event().wait()
        if outcome == "exception":
            raise OSError("synthetic sibling download failure")
        return None

    monkeypatch.setattr(pinterest, "_stream_download", download_item)
    task = asyncio.create_task(
        pinterest.PinterestNativeService.download_carousel(
            "https://pinterest.com/pin/123/"
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=2)
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "exception":
        with pytest.raises(OSError):
            await task
    else:
        paths, error = await task
        assert paths is None
        assert error

    assert list(tmp_path.iterdir()) == []


async def test_cancelled_pinterest_download_joins_disk_writer_before_removing_file(
    configure_stream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure, _ = configure_stream
    configure("pinterest", StreamResponse([b"abc"]))
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    original_open = builtins.open

    class BlockingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, chunk):
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("synthetic disk writer was not released")
                return self.handle.write(chunk)
            finally:
                finished.set()

        def close(self):
            self.handle.close()

    def open_file(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if Path(path).parent == tmp_path and mode in {"wb", "ab"}:
            return BlockingFile(handle)
        return handle

    monkeypatch.setattr(builtins, "open", open_file)
    task = asyncio.create_task(download("pinterest"))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation detached an active disk writer"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert list(tmp_path.iterdir()) == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 5)
