"""Tests for app.services.downloader — VideoDownloader + helpers."""

import asyncio
import os
import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class TestVideoDownloader(unittest.IsolatedAsyncioTestCase):
    """Test VideoDownloader.download_video."""

    async def asyncSetUp(self):
        from app.core import state as s

        s.file_cache = {}
        s.cancel_cache = {}
        s.ytdlp = MagicMock()
        s.ytdlp.build_command.return_value = ["echo", "test"]
        s.limiter = MagicMock()

    async def test_cached_file_returns_immediately(self):
        """If file is already in file_cache, return it without downloading."""
        from app.services.downloader import VideoDownloader
        from app.core import state as s
        import tempfile

        # Create a temp file to serve as "cached"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp.write(b"cached data")
        tmp.close()

        try:
            s.file_cache["tok123"] = tmp.name
            path, error = await VideoDownloader.download_video(
                "https://youtube.com/watch?v=abc",
                "137",
                1080,
                "tok123",
            )
            self.assertEqual(path, tmp.name)
            self.assertIsNone(error)
        finally:
            os.unlink(tmp.name)

    async def test_cached_file_not_on_disk_falls_through(self):
        """If file_cache points to nonexistent file, fall through to download."""
        from app.services.downloader import VideoDownloader
        from app.core import state as s

        s.file_cache["tok_ghost"] = "/nonexistent/file.mp4"

        # download will fail since build_command returns ["echo", "test"] which
        # doesn't produce a file — we just verify it doesn't return the ghost path
        path, error = await VideoDownloader.download_video(
            "https://youtube.com/watch?v=abc",
            "137",
            1080,
            "tok_ghost",
        )
        # The ghost path should NOT be returned
        self.assertNotEqual(path, "/nonexistent/file.mp4")


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("phase_offset", [0.0, 0.15, 0.35])
@pytest.mark.asyncio
async def test_token_cancellation_cleans_ytdlp_within_two_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_state,
    phase_offset: float,
):
    from app.core import state
    from app.core.process import process_supervisor
    from app.services import downloader

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    ready = tmp_path / "ready"

    def build_command(*_args, output: str, **_kwargs) -> list[str]:
        script = (
            "import pathlib,socket,sys,time;"
            "output=pathlib.Path(sys.argv[-1]);"
            "partial=output.with_name(output.stem+'.f137.mp4.part');"
            "partial.write_bytes(b'partial');"
            "s=socket.socket();"
            f"s.bind(('127.0.0.1',{port}));s.listen();"
            f"pathlib.Path({str(ready)!r}).touch();"
            "time.sleep(60)"
        )
        return [sys.executable, "-c", script, "--output", output]

    monkeypatch.setattr(downloader, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "YOUTUBE_PIPE_MODE", False)
    state.ytdlp = SimpleNamespace(build_command=build_command)
    token = f"cancel-{phase_offset}"
    task = asyncio.create_task(
        downloader.VideoDownloader.download_video(
            "https://example.invalid/watch",
            "137",
            720,
            token,
        )
    )
    try:
        async with asyncio.timeout(1):
            while not ready.exists():
                await asyncio.sleep(0.01)
        await asyncio.sleep(phase_offset)

        started = time.monotonic()
        await state.cancel_cache.set(token, True)
        await process_supervisor.cancel_owner(token)
        path, error = await asyncio.wait_for(
            task, timeout=max(0.1, 2 - (time.monotonic() - started))
        )

        assert time.monotonic() - started < 2
        assert path is None
        assert error == "❌ Загрузка отменена пользователем."
        assert not list(tmp_path.glob("ytdl_*"))
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
