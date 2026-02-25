import unittest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock modules before importing app.services.downloader
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.error"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["cachetools.TTLCache"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["aiohttp"] = MagicMock()
sys.modules["yt_dlp"] = MagicMock()

os.environ["BOT_TOKEN"] = "mock_token"

from app.services.downloader import MediaSender

class TestDownloaderTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_download_video_read_timeout_constant(self):
        # Mock run_subprocess context manager
        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock()
        mock_proc.returncode = 0

        mock_handle = AsyncMock()
        mock_handle.proc = mock_proc
        mock_handle.stderr_data = []

        with patch("app.services.downloader.state.ytdlp.build_command", return_value=["cmd"]), \
             patch("app.services.downloader.state.file_cache", {}), \
             patch("app.services.downloader.state.cancel_cache", {}), \
             patch("app.services.downloader.run_subprocess") as mock_run_subprocess, \
             patch("asyncio.wait_for") as mock_wait_for, \
             patch("app.services.downloader.DL_TIMEOUT_READ", 123.4):

            mock_run_subprocess.return_value.__aenter__.return_value = mock_handle

            # Setup wait_for to raise TimeoutError
            # To avoid RuntimeWarning about not awaiting coro, we can make side_effect handle it
            async def side_effect(coro, timeout):
                if asyncio.iscoroutine(coro):
                     # Just close it to avoid warning
                    coro.close()
                raise asyncio.TimeoutError()

            mock_wait_for.side_effect = side_effect

            # Execute
            await MediaSender.download_video("url", "fmt", 1080, "token")

            # Verify wait_for was called at least once
            self.assertTrue(mock_wait_for.called)

            # Check the timeout argument of the first call
            # It should be 123.4 as patched
            _, kwargs = mock_wait_for.call_args
            self.assertEqual(kwargs['timeout'], 123.4)

if __name__ == "__main__":
    unittest.main()
