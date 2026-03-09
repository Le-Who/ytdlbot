from unittest.mock import AsyncMock
"""Tests for app.services.tikwm — TikWMService.

Skipped if curl_cffi is not installed (it's optional, only needed at runtime).
"""

import unittest
from unittest.mock import patch, MagicMock
import os

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestTikWMFetchVideo(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.fetch_video."""

    async def test_success_returns_url_and_title(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "hdplay": "https://cdn.tikwm.com/video.mp4",
                "title": "Test Video",
                "duration": 30,
            },
        }

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            url, title, error = await TikWMService.fetch_video(
                "https://tiktok.com/@user/video/123"
            )

        self.assertEqual(url, "https://cdn.tikwm.com/video.mp4")
        self.assertEqual(title, "Test Video")
        self.assertIsNone(error)

    async def test_api_error_code_returns_error(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": -1, "msg": "video not found"}

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            url, title, error = await TikWMService.fetch_video(
                "https://tiktok.com/@user/video/123"
            )

        self.assertIsNone(url)
        self.assertIn("video not found", error)

    async def test_no_video_url_returns_error(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": {}}

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            url, title, error = await TikWMService.fetch_video(
                "https://tiktok.com/@user/video/123"
            )

        self.assertIsNone(url)
        self.assertIn("no video URL", error)

    async def test_network_error_retries(self):
        from app.services.tikwm import TikWMService

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            with patch("app.services.tikwm.MAX_RETRIES", 2):
                url, title, error = await TikWMService.fetch_video(
                    "https://tiktok.com/@user/video/123"
                )

        self.assertIsNone(url)
        self.assertIn("error", error.lower())
        self.assertEqual(mock_session.get.await_count, 2)


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestTikWMDownloadVideo(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.download_video."""

    async def test_fetch_error_propagated(self):
        from app.services.tikwm import TikWMService

        with patch.object(
            TikWMService,
            "fetch_video",
            new_callable=AsyncMock,
            return_value=(None, None, "TikWM: not found"),
        ):
            path, error = await TikWMService.download_video(
                "https://tiktok.com/@user/video/123"
            )

        self.assertIsNone(path)
        self.assertEqual(error, "TikWM: not found")

    async def test_download_success_writes_file(self):
        from app.services.tikwm import TikWMService
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
            "fetch_video",
            new_callable=AsyncMock,
            return_value=("https://cdn.tikwm.com/video.mp4", "Title", None),
        ):
            with patch("app.services.tikwm.TEMP_DIR", tmpdir):
                with patch(
                    "app.services.tikwm.AsyncSession", return_value=mock_session
                ):
                    path, error = await TikWMService.download_video(
                        "https://tiktok.com/video/123"
                    )

        self.assertIsNone(error)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        os.unlink(path)
        os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
