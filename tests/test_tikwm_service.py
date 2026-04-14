from unittest.mock import AsyncMock, patch, MagicMock
import unittest
import os
import tempfile

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestTikWMProcess(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.process."""

    async def test_success_returns_video_result(self):
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
            res = await TikWMService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "video")
        self.assertEqual(res.url, "https://cdn.tikwm.com/video.mp4")
        self.assertEqual(res.title, "Test Video")
        self.assertFalse(res.is_slideshow)
        self.assertIsNone(res.error_message)

    async def test_success_returns_slideshow_result(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "images": ["url1", "url2"],
                "music": "https://cdn.tikwm.com/music.mp3",
                "title": "Test Slideshow",
            },
        }

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            res = await TikWMService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "picker")
        self.assertEqual(res.images, ["url1", "url2"])
        self.assertEqual(res.audio_url, "https://cdn.tikwm.com/music.mp3")
        self.assertEqual(res.title, "Test Slideshow")
        self.assertTrue(res.is_slideshow)
        self.assertIsNone(res.error_message)

    async def test_api_error_code_returns_error(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": -1, "msg": "video not found"}

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            res = await TikWMService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "error")
        self.assertIn("video not found", res.error_message)

    async def test_no_video_url_returns_error(self):
        from app.services.tikwm import TikWMService

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": {}}

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            res = await TikWMService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "error")
        self.assertIn("No video or images", res.error_message)

    async def test_network_error_retries(self):
        from app.services.tikwm import TikWMService

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("app.services.tikwm.AsyncSession", return_value=mock_session):
            with patch("app.services.tikwm.MAX_RETRIES", 2):
                res = await TikWMService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "error")
        self.assertIn("timeout", res.error_message.lower())
        self.assertEqual(mock_session.get.await_count, 2)


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestTikWMDownloadVideo(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.download_video."""

    async def test_process_error_propagated(self):
        from app.services.tikwm import TikWMService, TikWMResult

        with patch.object(
            TikWMService,
            "process",
            new_callable=AsyncMock,
            return_value=TikWMResult(status="error", error_message="TikWM: not found"),
        ):
            path, error = await TikWMService.download_video(
                "https://tiktok.com/@user/video/123"
            )

        self.assertIsNone(path)
        self.assertEqual(error, "TikWM: not found")

    async def test_download_success_writes_file(self):
        from app.services.tikwm import TikWMService, TikWMResult

        tmpdir = tempfile.mkdtemp()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"fake video data" * 1024

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
