import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import asyncio
import os

# Mock dependencies
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.error"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["dotenv"] = MagicMock()
sys.modules["yt_dlp"] = MagicMock()

# Now import the module under test
from app.services.downloader import MediaSender
from app.core import state

class TestMediaSender(unittest.IsolatedAsyncioTestCase):
    async def test_download_video_cache_hit_uses_to_thread(self):
        token = "test_token"
        cached_path = "/tmp/cached_file.mp4"

        # Mock state.file_cache
        state.file_cache = MagicMock()
        state.file_cache.get.return_value = cached_path

        with patch("app.services.downloader.os.path.exists") as mock_exists:
             mock_exists.return_value = True

             with patch("app.services.downloader.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
                mock_to_thread.return_value = True

                result_path, error = await MediaSender.download_video(
                    "http://example.com", "format_id", 1080, token
                )

                self.assertEqual(result_path, cached_path)
                self.assertIsNone(error)

                # Verify to_thread was called with os.path.exists and cached_path
                mock_to_thread.assert_called_with(os.path.exists, cached_path)

if __name__ == "__main__":
    unittest.main()
