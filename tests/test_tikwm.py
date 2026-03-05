"""Tests for TikWMService async curl_cffi implementation."""
import importlib
import importlib.util
import os
import sys
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

_has_curl_cffi = importlib.util.find_spec("curl_cffi") is not None


class MockResponse:
    """Minimal mock for curl_cffi Response."""

    def __init__(self, data: dict | None = None, content: bytes = b"", status_code: int = 200):
        self._data = data
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._data


@unittest.skipUnless(_has_curl_cffi, "curl_cffi not installed (Docker-only dependency)")
class TestTikWMServiceFetchVideo(unittest.IsolatedAsyncioTestCase):
    """Tests for TikWMService.fetch_video (async)."""

    def setUp(self):
        from app.services.tikwm import TikWMService
        self.TikWMService = TikWMService

    async def test_fetch_video_success_hdplay(self):
        """Successful fetch with hdplay URL."""
        mock_resp = MockResponse(data={
            "code": 0,
            "data": {
                "hdplay": "https://cdn.tikwm.com/video/hd/123.mp4",
                "play": "https://cdn.tikwm.com/video/sd/123.mp4",
                "title": "Test TikTok Video",
                "duration": 15,
            }
        })

        with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.get = AsyncMock(return_value=mock_resp)
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = session_mock

            video_url, title, error = await self.TikWMService.fetch_video(
                "https://www.tiktok.com/@user/video/123"
            )

            self.assertIsNone(error)
            self.assertEqual(video_url, "https://cdn.tikwm.com/video/hd/123.mp4")
            self.assertEqual(title, "Test TikTok Video")

    async def test_fetch_video_success_play_fallback(self):
        """When hdplay is missing, falls back to play URL."""
        mock_resp = MockResponse(data={
            "code": 0,
            "data": {
                "hdplay": None,
                "play": "https://cdn.tikwm.com/video/sd/456.mp4",
                "title": "SD Video",
                "duration": 30,
            }
        })

        with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.get = AsyncMock(return_value=mock_resp)
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = session_mock

            video_url, title, error = await self.TikWMService.fetch_video(
                "https://www.tiktok.com/@user/video/456"
            )

            self.assertIsNone(error)
            self.assertEqual(video_url, "https://cdn.tikwm.com/video/sd/456.mp4")

    async def test_fetch_video_api_error_code(self):
        """API returns non-zero code → error message."""
        mock_resp = MockResponse(data={
            "code": -1,
            "msg": "Video not found",
        })

        with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.get = AsyncMock(return_value=mock_resp)
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = session_mock

            video_url, title, error = await self.TikWMService.fetch_video(
                "https://www.tiktok.com/@user/video/invalid"
            )

            self.assertIsNone(video_url)
            self.assertIsNone(title)
            self.assertIn("Video not found", error)

    async def test_fetch_video_no_url_in_response(self):
        """API returns success but no video URL."""
        mock_resp = MockResponse(data={
            "code": 0,
            "data": {"title": "No Video"},
        })

        with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.get = AsyncMock(return_value=mock_resp)
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = session_mock

            video_url, title, error = await self.TikWMService.fetch_video(
                "https://www.tiktok.com/@user/video/no_url"
            )

            self.assertIsNone(video_url)
            self.assertIn("no video URL", error)

    async def test_fetch_video_network_error_retries(self):
        """Network error triggers retry up to MAX_RETRIES times."""
        with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.get = AsyncMock(side_effect=ConnectionError("Network down"))
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = session_mock

            video_url, title, error = await self.TikWMService.fetch_video(
                "https://www.tiktok.com/@user/video/retry"
            )

            self.assertIsNone(video_url)
            self.assertIn("Network down", error)
            # Should have been called MAX_RETRIES times (3)
            self.assertEqual(session_mock.get.await_count, 3)


@unittest.skipUnless(_has_curl_cffi, "curl_cffi not installed (Docker-only dependency)")
class TestTikWMServiceDownloadVideo(unittest.IsolatedAsyncioTestCase):
    """Tests for TikWMService.download_video (async)."""

    def setUp(self):
        from app.services.tikwm import TikWMService
        self.TikWMService = TikWMService

    async def test_download_video_success(self):
        """Full download pipeline: fetch URL → download content → save file."""
        # Mock the fetch_video call
        with patch.object(
            self.TikWMService, "fetch_video",
            new_callable=AsyncMock,
            return_value=("https://cdn.tikwm.com/video/hd/123.mp4", "Test", None),
        ):
            # Mock the download session
            with patch("app.services.tikwm.AsyncSession") as mock_session_cls:
                session_mock = AsyncMock()
                session_mock.get = AsyncMock(
                    return_value=MockResponse(content=b"\x00" * 1024)
                )
                session_mock.__aenter__ = AsyncMock(return_value=session_mock)
                session_mock.__aexit__ = AsyncMock(return_value=None)
                mock_session_cls.return_value = session_mock

                file_path, error = await self.TikWMService.download_video(
                    "https://www.tiktok.com/@user/video/123"
                )

                self.assertIsNone(error)
                self.assertIsNotNone(file_path)
                # Clean up
                if file_path and os.path.exists(file_path):
                    os.unlink(file_path)

    async def test_download_video_fetch_fails(self):
        """If fetch_video fails, download_video returns the error."""
        with patch.object(
            self.TikWMService, "fetch_video",
            new_callable=AsyncMock,
            return_value=(None, None, "TikWM: Video not found"),
        ):
            file_path, error = await self.TikWMService.download_video(
                "https://www.tiktok.com/@user/video/missing"
            )

            self.assertIsNone(file_path)
            self.assertEqual(error, "TikWM: Video not found")


if __name__ == "__main__":
    unittest.main()
