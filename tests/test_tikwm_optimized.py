import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import os
import sys

# Mocking all missing dependencies
sys.modules["dotenv"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["curl_cffi"] = MagicMock()
sys.modules["curl_cffi.requests"] = MagicMock()
sys.modules["cachetools"] = MagicMock()

# Mock curl_cffi BEFORE importing TikWMService
mock_curl = MagicMock()
mock_curl.requests.AsyncSession = AsyncMock
sys.modules["curl_cffi"] = mock_curl
sys.modules["curl_cffi.requests"] = mock_curl.requests

from app.services.tikwm import TikWMService, TikWMResult


class TestTikWMDownloadVideoOptimized(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.download_video with optimized async I/O."""

    async def test_download_success_writes_file_async(self):
        import tempfile

        tmpdir = tempfile.mkdtemp()

        mock_resp = MagicMock()
        mock_resp.content = b"fake video data"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch.object(
            TikWMService,
            "process",
            new_callable=AsyncMock,
            return_value=TikWMResult(
                status="video", url="https://cdn.tikwm.com/video.mp4", title="Title"
            ),
        ):
            with patch("app.services.tikwm.TEMP_DIR", tmpdir):
                with patch(
                    "app.services.tikwm.AsyncSession", return_value=mock_session
                ):
                    with patch("uuid.uuid4") as mock_uuid:
                        mock_uuid.return_value.hex = "test"
                        expected_path = os.path.join(tmpdir, "tikwm_test.mp4")

                        path, error = await TikWMService.download_video(
                            "https://tiktok.com/video/123"
                        )

        self.assertIsNone(error)
        self.assertEqual(path, expected_path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"fake video data")

        os.unlink(path)
        os.rmdir(tmpdir)

    async def test_download_failure_cleanup_async(self):
        import tempfile

        tmpdir = tempfile.mkdtemp()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        # Force an error during download
        mock_session.get = AsyncMock(side_effect=Exception("Download failed"))

        with patch.object(
            TikWMService,
            "process",
            new_callable=AsyncMock,
            return_value=TikWMResult(
                status="video", url="https://cdn.tikwm.com/video.mp4", title="Title"
            ),
        ):
            with patch("app.services.tikwm.TEMP_DIR", tmpdir):
                with patch(
                    "app.services.tikwm.AsyncSession", return_value=mock_session
                ):
                    with patch("uuid.uuid4") as mock_uuid:
                        mock_uuid.return_value.hex = "failure_test"
                        path, error = await TikWMService.download_video(
                            "https://tiktok.com/video/123"
                        )

        self.assertIsNone(path)
        self.assertIn("Download failed", error)

        expected_path = os.path.join(tmpdir, "tikwm_failure_test.mp4")
        self.assertFalse(os.path.exists(expected_path))
        os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
