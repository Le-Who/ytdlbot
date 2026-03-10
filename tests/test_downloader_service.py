"""Tests for app.services.downloader — VideoDownloader + helpers."""

import unittest
from unittest.mock import MagicMock
import os


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
