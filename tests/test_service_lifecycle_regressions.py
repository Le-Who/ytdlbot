import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.models import DownloadContext


@pytest.mark.parametrize("cancel_download", [False, True])
async def test_downloader_joins_progress_callbacks_before_return(
    tmp_path, monkeypatch, mock_state, cancel_download
):
    from app.services import downloader

    callback_started = asyncio.Event()
    callback_stopped = asyncio.Event()
    callback_tasks = []

    async def progress(*args):
        callback_tasks.append(asyncio.current_task())
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_stopped.set()

    async def readline():
        if cancel_download:
            await asyncio.Event().wait()
        return b""

    @asynccontextmanager
    async def run(command, *, stderr_callback, **kwargs):
        Path(command[-1]).write_bytes(b"video")
        stderr_callback(b"[download] 50.0% of 1 MiB at 1 MiB/s ETA 00:01")
        await callback_started.wait()
        yield SimpleNamespace(
            proc=SimpleNamespace(
                stdout=SimpleNamespace(readline=readline),
                returncode=0,
                wait=AsyncMock(return_value=0),
            ),
            stderr_data=[],
        )

    monkeypatch.setattr(downloader, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "YOUTUBE_PIPE_MODE", False)
    monkeypatch.setattr(mock_state, "file_cache", {})
    monkeypatch.setattr(downloader, "run_subprocess", run)
    mock_state.ytdlp.build_command.side_effect = lambda *args, output, **kwargs: [
        "test",
        output,
    ]
    task = asyncio.create_task(
        downloader.VideoDownloader.download_video(
            "https://example.com/video", "18", 720, "token", progress_callback=progress
        )
    )
    try:
        await asyncio.wait_for(callback_started.wait(), 1)
        if cancel_download:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            path, error = await asyncio.wait_for(task, 1)
            assert path and error is None
        assert callback_stopped.is_set(), (
            "download returned with an active progress callback"
        )
        assert all(task.done() for task in callback_tasks)
    finally:
        task.cancel()
        for callback in callback_tasks:
            callback.cancel()
        await asyncio.gather(task, *callback_tasks, return_exceptions=True)


@pytest.mark.parametrize("part_count", [1, 3])
async def test_orchestrator_cleans_every_split_part_on_send_cancellation(
    tmp_path, monkeypatch, mock_state, part_count
):
    from app.services import orchestrator

    original = tmp_path / "source.mp4"
    original.write_bytes(b"source")
    parts = [tmp_path / f"part-{index}.mp4" for index in range(part_count)]
    for part in parts:
        part.write_bytes(b"part")
    send_started = asyncio.Event()

    async def send(*args, **kwargs):
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mock_state, "media_pipeline", None)
    monkeypatch.setattr(
        mock_state,
        "download_queue",
        SimpleNamespace(enqueue=AsyncMock(return_value=True), release=Mock()),
    )
    monkeypatch.setattr(orchestrator.config, "TELEGRAM_LOCAL_ENDPOINT", "")
    monkeypatch.setattr(orchestrator, "_TG_UPLOAD_LIMIT_BYTES", 1)
    monkeypatch.setattr(
        orchestrator,
        "ensure_telegram_compatible",
        AsyncMock(side_effect=lambda path, **kw: path),
    )
    monkeypatch.setattr(
        orchestrator,
        "split_video_stream_copy",
        AsyncMock(return_value=[str(part) for part in parts]),
    )
    monkeypatch.setattr(
        orchestrator, "compress_video_to_size", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(orchestrator, "extract_video_meta", AsyncMock(return_value={}))
    monkeypatch.setattr(
        orchestrator.MediaSender,
        "download_video",
        AsyncMock(return_value=(str(original), None)),
    )
    monkeypatch.setattr(orchestrator.MediaSender, "send_file", send)
    task = asyncio.create_task(
        orchestrator.DownloadOrchestrator._process_download_owned(
            "token",
            1,
            SimpleNamespace(send_chat_action=AsyncMock()),
            DownloadContext("https://example.com/video", "18"),
            None,
            AsyncMock(),
            None,
        )
    )
    try:
        await asyncio.wait_for(send_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not part.exists() for part in parts), (
            "unsent split parts lost their cleanup owner"
        )
        mock_state.download_queue.release.assert_called_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_instagram_session_evicted_during_limiter_wait_is_not_reused(
    monkeypatch, mock_state
):
    from app.services.instagram import InstagramService

    waiting = asyncio.Event()
    resume = asyncio.Event()
    calls = []

    async def allow(index):
        calls.append(index)
        if index == 0:
            waiting.set()
            await resume.wait()
        return True

    monkeypatch.setattr(InstagramService, "_pool_initialized", True)
    monkeypatch.setattr(
        InstagramService,
        "_ig_cookie_pool",
        [{"sessionid": "dead"}, {"sessionid": "live"}],
    )
    monkeypatch.setattr(InstagramService, "_current_pool_index", 0)
    monkeypatch.setattr(InstagramService, "_dead_sessions", set())
    mock_state.limiter.allow_ig_fallback = allow
    task = asyncio.create_task(InstagramService._get_available_session())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        InstagramService._check_response_health(403, "checkpoint_required", 0)
        resume.set()
        index, cookies = await asyncio.wait_for(task, 1)
        assert index == 1
        assert cookies == {"sessionid": "live"}
        assert calls == [0, 1]
    finally:
        resume.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("method", ["download_video", "download_slideshow"])
@pytest.mark.parametrize("failure", ["missing", "failed", "empty", "cancel"])
async def test_gallery_failure_retains_no_unpublished_directory(
    tmp_path, monkeypatch, method, failure
):
    from app.services.gallery_dl import service

    async def run(command, **kwargs):
        output = Path(command[command.index("--directory") + 1])
        (output / "media.mp4.part").write_bytes(b"partial")
        if failure == "missing":
            raise FileNotFoundError("gallery-dl")
        if failure == "cancel":
            raise asyncio.CancelledError
        return SimpleNamespace(
            returncode=1 if failure == "failed" else 0, stderr=b"failed"
        )

    monkeypatch.setattr(service, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(service.process_supervisor, "run", run)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await getattr(service.GalleryDlService, method)("https://example.com/video")
    else:
        result, error = await getattr(service.GalleryDlService, method)(
            "https://example.com/video"
        )
        assert result is None and error
    assert not list(tmp_path.iterdir()), (
        "failed extraction retained an ownerless directory"
    )
