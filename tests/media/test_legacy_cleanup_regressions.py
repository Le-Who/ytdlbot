from __future__ import annotations

import asyncio
import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from app.core.media_cache import MediaCache
from app.services.cobalt import CobaltPickerItem, CobaltResult, CobaltService
from app.services.media.delivery import TelegramDelivery
from app.services.media.models import (
    DeliveryTarget,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
    MediaSource,
)
from app.services.media.pipeline import MediaPipeline, MediaResolutionError
from app.services.media.registry import ProviderRegistry
from app.services.media.transport import (
    DownloadFailed,
    MaterializedItem,
    MediaTransport,
    TransferTimeout,
)


@pytest.mark.asyncio
async def test_direct_candidate_selection_preserves_order_and_reuses_file_ids(tmp_path):
    items = tuple(
        MediaItem(str(index), MediaKind.PHOTO, f"https://cdn.example/{index}.jpg")
        for index in range(3)
    )
    request = MediaRequest(
        "https://instagram.com/p/album/",
        "instagram",
        "album",
        album_selection=(2, 0),
        auth_scope="instagram:owner",
    )
    candidate = MediaCandidate(
        "album",
        items[0].url,
        kind=MediaKind.ALBUM,
        media_id="album",
        items=items,
        sources=tuple(MediaSource(str(i), item.url) for i, item in enumerate(items)),
        auth_scope=request.auth_scope,
    )
    reservation = SimpleNamespace(release=AsyncMock(), renew=lambda: None)

    async def materialize(work_request, candidates, **kwargs):
        del kwargs
        assert work_request.cache_key == request.cache_key
        selected = candidates[0]
        assert tuple(item.media_id for item in selected.items) == ("2", "0")
        assert tuple(source.url for source in selected.sources) == (
            items[2].url,
            items[0].url,
        )
        paths = tuple(tmp_path / f"{i}.jpg" for i in range(2))
        for path in paths:
            path.write_bytes(b"photo")
        return MaterializedItem(paths, 10, selected, reservation)

    transport = SimpleNamespace(materialize=AsyncMock(side_effect=materialize))
    bot = SimpleNamespace(
        id=123,
        send_media_group=AsyncMock(
            return_value=tuple(
                SimpleNamespace(
                    message_id=i + 1,
                    photo=[
                        SimpleNamespace(
                            file_id=f"file-{i}", file_unique_id=f"unique-{i}"
                        )
                    ],
                )
                for i in range(2)
            )
        ),
    )
    cache = MediaCache()
    pipeline = MediaPipeline(
        ProviderRegistry(),
        transport,
        TelegramDelivery(
            bot, media_dir=tmp_path, local_mode=True, media_cache=cache, bot_id="123"
        ),
        artifact_validator=AsyncMock(),
    )
    target = DeliveryTarget("1", auth_scope=request.auth_scope)

    first = await pipeline.deliver_candidate(request, target, candidate)
    second = await pipeline.deliver_candidate(request, target, candidate)

    assert first.success and second.success
    assert tuple(item.item.media_id for item in first.items) == ("2", "0")
    assert tuple(item.item_index for item in second.items) == (0, 1)
    assert tuple(
        media.media for media in bot.send_media_group.await_args.kwargs["media"]
    ) == ("file-0", "file-1")
    transport.materialize.assert_awaited_once()
    reservation.release.assert_awaited_once()
    assert not list(tmp_path.glob("*.jpg"))


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [(-1,), (3,)])
async def test_direct_candidate_rejects_invalid_source_indexes(selection):
    request = MediaRequest(
        "https://example.com/album", "other", "album", album_selection=selection
    )
    item = MediaItem("0", MediaKind.PHOTO, "https://cdn.example/0.jpg")
    candidate = MediaCandidate("album", item.url, items=(item,), kind=MediaKind.ALBUM)
    transport = SimpleNamespace(materialize=AsyncMock())
    pipeline = MediaPipeline(
        ProviderRegistry(), transport, SimpleNamespace(), artifact_validator=AsyncMock()
    )

    with pytest.raises(MediaResolutionError):
        await pipeline.deliver_candidate(request, DeliveryTarget("1"), candidate)

    transport.materialize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_cobalt_slideshow_joins_siblings_before_cleanup(
    tmp_path, monkeypatch, cancel_parent
):
    from app.services import cobalt

    monkeypatch.setattr(cobalt, "TEMP_DIR", str(tmp_path))
    streaming = asyncio.Event()
    sibling_stopped = asyncio.Event()
    finish_sibling = asyncio.Event()
    sessions = []

    class Session:
        def __init__(self, **kwargs):
            del kwargs
            self.closed = False
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def get(self, url, **kwargs):
            del kwargs
            if url.endswith("fail"):
                await streaming.wait()
                if cancel_parent:
                    await asyncio.Event().wait()
                return SimpleNamespace(status_code=500)
            return SimpleNamespace(status_code=200)

    async def stream(response, output_path):
        del response
        Path(output_path).write_bytes(b"partial")
        streaming.set()
        try:
            await finish_sibling.wait()
        finally:
            sibling_stopped.set()
        return 7

    monkeypatch.setattr(cobalt, "AsyncSession", Session)
    monkeypatch.setattr(cobalt, "_stream_response", stream)
    result = CobaltResult(
        "picker",
        picker=[
            CobaltPickerItem("photo", "https://example.com/fail"),
            CobaltPickerItem("photo", "https://example.com/stream"),
        ],
    )
    task = asyncio.create_task(CobaltService.download_slideshow(result))
    try:
        await asyncio.wait_for(streaming.wait(), 1)
        if cancel_parent:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await asyncio.wait_for(task, 1) == (None, None)
        assert sibling_stopped.is_set(), "slideshow returned with an active download"
        assert all(session.closed for session in sessions)
        assert not list(tmp_path.iterdir())
    finally:
        finish_sibling.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["cobalt", "tikwm"])
async def test_legacy_stream_cancellation_joins_owned_write(
    tmp_path, monkeypatch, module_name
):
    module = importlib.import_module(f"app.services.{module_name}")
    writing = threading.Event()
    allow_write = threading.Event()
    closed = threading.Event()

    class Output:
        def write(self, chunk):
            writing.set()
            assert allow_write.wait(5)
            assert not closed.is_set(), "stream closed while its write still ran"
            return len(chunk)

        def close(self):
            closed.set()

    class Response:
        headers: ClassVar[dict[str, str]] = {}

        async def aiter_content(self, **kwargs):
            del kwargs
            yield b"media"

    monkeypatch.setattr(module, "open", lambda *args: Output(), raising=False)
    task = asyncio.create_task(
        module._stream_response(Response(), str(tmp_path / "output"))
    )
    try:
        assert await asyncio.to_thread(writing.wait, 1)
        task.cancel()
        await asyncio.wait((task,), timeout=0.05)
        assert not task.done(), "stream returned while its write thread still ran"
        assert not closed.is_set()
        task.cancel()
    finally:
        allow_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["cobalt", "tikwm"])
async def test_legacy_file_download_cancellation_removes_partial(
    tmp_path, monkeypatch, module_name
):
    module = importlib.import_module(f"app.services.{module_name}")
    monkeypatch.setattr(module, "TEMP_DIR", str(tmp_path))
    streaming = asyncio.Event()

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {}

        async def aiter_content(self, **kwargs):
            del kwargs
            yield b"partial"
            streaming.set()
            await asyncio.Event().wait()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(module, "AsyncSession", Session)
    download = (
        module.CobaltService.download_file("https://example.com/media", "mp4")
        if module_name == "cobalt"
        else module.TikWMService.download_audio("https://example.com/media")
    )
    task = asyncio.create_task(download)
    await asyncio.wait_for(streaming.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared, encoding", [(None, None), (17, None), (100, None), (10, "gzip")]
)
async def test_transport_checks_declared_length_before_publication(
    tmp_path, declared, encoding
):
    from app.services.media.transport import _Opened

    body = b"\x00\x00\x00\x18ftypisommedia"

    class Response:
        status_code = 200
        close = AsyncMock()

        def __init__(self):
            self.headers = {} if declared is None else {"Content-Length": str(declared)}
            if encoding is not None:
                self.headers["Content-Encoding"] = encoding

        async def iter_bytes(self, chunk_size):
            del chunk_size
            yield body

    media = MediaTransport(output_dir=tmp_path)
    response = Response()
    media._request = AsyncMock(
        return_value=_Opened(response, "https://cdn.example/media", time.monotonic())
    )
    reservation = SimpleNamespace(
        bind=lambda path: None,
        ensure=AsyncMock(),
        promote=lambda source, destination: source.rename(destination),
    )
    operation = media._stream_source(
        MediaSource("0", "https://cdn.example/media"),
        reservation,
        already_written=0,
        deadline=time.monotonic() + 10,
    )
    if declared == 100:
        with pytest.raises(DownloadFailed, match="truncated"):
            await operation
        assert not list(tmp_path.iterdir())
    else:
        path, count = await operation
        assert count == len(body)
        assert path.read_bytes() == body
    response.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["duration", "concat"])
async def test_slideshow_converter_joins_owned_disk_work_on_cancel(
    tmp_path, monkeypatch, phase
):
    from app.services import converter

    monkeypatch.setattr(converter, "TEMP_DIR", str(tmp_path))
    started = threading.Event()
    finish = threading.Event()
    original_open = open

    def wait_for_disk():
        started.set()
        assert finish.wait(5)

    def duration(path):
        del path
        wait_for_disk()
        return 10

    def open_concat(path, *args, **kwargs):
        if Path(path).name.startswith("concat_"):
            wait_for_disk()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(converter, "_get_audio_duration", duration)
    monkeypatch.setattr(converter, "open", open_concat, raising=False)
    audio = tmp_path / "track.mp3"
    if phase == "duration":
        audio.write_bytes(b"audio")
    task = asyncio.create_task(
        converter.MediaConverter.images_to_video(
            [str(tmp_path / "image.jpg")],
            str(audio) if phase == "duration" else None,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.wait((task,), timeout=0.05)
        assert not task.done(), (
            "converter released caller-owned input or temporary path before disk work ended"
        )
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list(tmp_path.glob("concat_*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["images", "audio"])
async def test_tikwm_slideshow_cancellation_removes_all_owned_outputs(
    tmp_path, monkeypatch, phase
):
    from app.services import tikwm

    monkeypatch.setattr(tikwm, "TEMP_DIR", str(tmp_path))
    blocked = asyncio.Event()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            return SimpleNamespace(status_code=200, headers={}, url=url)

    async def stream(response, output_path):
        Path(output_path).write_bytes(b"x" * 128)
        if phase == "images" and response.url.endswith("second"):
            blocked.set()
            await asyncio.Event().wait()
        return 128

    async def download_audio(url):
        del url
        blocked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tikwm, "AsyncSession", Session)
    monkeypatch.setattr(tikwm, "_stream_response", stream)
    monkeypatch.setattr(tikwm.TikWMService, "download_audio", download_audio)
    result = tikwm.TikWMResult(
        "picker",
        images=["https://example.com/first", "https://example.com/second"],
        audio_url="https://example.com/audio",
    )
    task = asyncio.create_task(tikwm.TikWMService.download_slideshow(result))
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_transport_stream_joins_write_before_closing_or_deleting(
    tmp_path, monkeypatch, cancel_parent
):
    from app.services.media.transport import _Opened

    writing = threading.Event()
    allow_write = threading.Event()
    closed = threading.Event()
    real_open = Path.open

    class Output:
        def __init__(self, handle):
            self.handle = handle

        def write(self, chunk):
            writing.set()
            assert allow_write.wait(5)
            assert not closed.is_set()
            return self.handle.write(chunk)

        def close(self):
            closed.set()
            self.handle.close()

    def open_output(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return Output(handle) if path.suffix == ".part" else handle

    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "video/mp4"}
        close = AsyncMock()

        async def iter_bytes(self, chunk_size):
            del chunk_size
            yield b"\x00\x00\x00\x18ftypisommedia"

    media = MediaTransport(output_dir=tmp_path)
    response = Response()
    media._request = AsyncMock(
        return_value=_Opened(response, "https://cdn.example/media", time.monotonic())
    )
    reservation = SimpleNamespace(bind=lambda path: None, ensure=AsyncMock())
    monkeypatch.setattr(Path, "open", open_output)
    task = asyncio.create_task(
        media._stream_source(
            MediaSource("0", "https://cdn.example/media"),
            reservation,
            already_written=0,
            deadline=time.monotonic() + (10 if cancel_parent else 0.1),
        )
    )
    try:
        assert await asyncio.to_thread(writing.wait, 1)
        if cancel_parent:
            task.cancel()
        await asyncio.wait((task,), timeout=0.15)
        assert not task.done(), "transport returned with its file write still running"
        assert not closed.is_set()
        response.close.assert_not_awaited()
    finally:
        allow_write.set()
        with pytest.raises(
            asyncio.CancelledError if cancel_parent else TransferTimeout
        ):
            await task
    assert closed.is_set()
    assert not list(tmp_path.iterdir())
