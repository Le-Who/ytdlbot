"""Tests for app.services.downloader — VideoDownloader + helpers."""

import asyncio
import os
import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestVideoDownloader(unittest.IsolatedAsyncioTestCase):
    """Test VideoDownloader.download_video."""

    async def asyncSetUp(self):
        from app.core import state as s
        from app.core.storage.memory import MemoryStorage

        for name, value in {
            "file_cache": {},
            "cancel_cache": MemoryStorage(maxsize=100, ttl=60),
            "ytdlp": MagicMock(),
            "limiter": MagicMock(),
        }.items():
            patcher = patch.object(s, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        s.ytdlp.build_command.return_value = ["echo", "test"]

    async def test_cached_file_returns_immediately(self):
        """If file is already in file_cache, return it without downloading."""
        import tempfile

        from app.core import state as s
        from app.services.downloader import VideoDownloader

        # Create a temp file to serve as "cached"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(b"cached data")

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
        """A ghost cache entry must be replaced by a successful disk download."""
        import tempfile

        from app.core import state as s
        from app.services import downloader

        s.file_cache["tok_ghost"] = "/nonexistent/file.mp4"

        def command(*args, output, **kwargs):
            return [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'fresh download')",
                output,
            ]

        s.ytdlp.build_command.side_effect = command
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(downloader, "TEMP_DIR", directory),
            patch.object(downloader, "YOUTUBE_PIPE_MODE", False),
        ):
            path, error = await downloader.VideoDownloader.download_video(
                "https://youtube.com/watch?v=abc",
                "137",
                1080,
                "tok_ghost",
            )
            self.assertIsNone(error)
            self.assertIsInstance(path, str)
            self.assertEqual(Path(path).read_bytes(), b"fresh download")
            self.assertEqual(s.file_cache["tok_ghost"], path)
            s.ytdlp.build_command.assert_called_once()
            self.assertEqual(s.ytdlp.build_command.call_args.kwargs["output"], path)


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
