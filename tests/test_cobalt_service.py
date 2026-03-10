import unittest
from unittest.mock import patch, MagicMock, AsyncMock

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestCobaltService(unittest.IsolatedAsyncioTestCase):
    async def test_process_video_tunnel_success(self):
        from app.services.cobalt import CobaltService

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "tunnel",
            "url": "https://cobalt.tools/api/tunnel/123",
            "filename": "tiktok_video.mp4",
        }

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.COBALT_API_URLS", ["https://mock.cobalt.test"]),
        ):
            res = await CobaltService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "tunnel")
        self.assertEqual(res.url, "https://cobalt.tools/api/tunnel/123")
        self.assertFalse(res.is_slideshow)

    async def test_process_slideshow_picker_success(self):
        from app.services.cobalt import CobaltService

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "picker",
            "audio": "https://cobalt.tools/api/audio/123",
            "picker": [
                {"type": "photo", "url": "img1.jpg"},
                {"type": "photo", "url": "img2.jpg"},
            ],
        }

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.COBALT_API_URLS", ["https://mock.cobalt.test"]),
        ):
            res = await CobaltService.process("https://tiktok.com/@user/photo/123")

        self.assertEqual(res.status, "picker")
        self.assertTrue(res.is_slideshow)
        self.assertEqual(res.audio, "https://cobalt.tools/api/audio/123")
        self.assertEqual(len(res.picker), 2)
        self.assertEqual(res.picker[0].type, "photo")

    async def test_process_error_response(self):
        from app.services.cobalt import CobaltService

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "error",
            "error": {"code": "not_found"},
        }

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.COBALT_API_URLS", ["https://mock.cobalt.test"]),
        ):
            res = await CobaltService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "error")
        if res.error_message:
            self.assertIn("not_found", res.error_message)

    async def test_process_network_error_retries(self):
        from app.services.cobalt import CobaltService

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = AsyncMock(side_effect=ConnectionError("timeout"))

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.COBALT_API_URLS", ["https://mock.cobalt.test"]),
        ):
            res = await CobaltService.process("https://tiktok.com/@user/video/123")

        self.assertEqual(res.status, "error")
        self.assertEqual(mock_session.post.await_count, 2)

    async def test_download_file_success(self):
        from app.services.cobalt import CobaltService

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"video_data"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.open", unittest.mock.mock_open()) as m_open,
        ):
            path = await CobaltService.download_file(
                "https://cdn.example.com/video.mp4", "mp4"
            )

            self.assertIsNotNone(path)
            if path:
                self.assertTrue(path.endswith(".mp4"))
            m_open().write.assert_called_once_with(b"video_data")

    async def test_download_slideshow_success(self):
        from app.services.cobalt import CobaltService, CobaltResult, CobaltPickerItem

        mock_resp_img = MagicMock()
        mock_resp_img.status_code = 200
        mock_resp_img.content = b"img_data"

        mock_resp_audio = MagicMock()
        mock_resp_audio.status_code = 200
        mock_resp_audio.content = b"audio_data"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _mock_get(url, **kwargs):
            if "audio" in url:
                return mock_resp_audio
            return mock_resp_img

        mock_session.get = AsyncMock(side_effect=_mock_get)

        res = CobaltResult(
            status="picker",
            audio="https://cdn.example.com/audio.mp3",
            picker=[
                CobaltPickerItem(type="photo", url="img1.jpg", thumb=""),
                CobaltPickerItem(type="photo", url="img2.jpg", thumb=""),
            ],
        )

        with (
            patch("app.services.cobalt.AsyncSession", return_value=mock_session),
            patch("app.services.cobalt.open", unittest.mock.mock_open()) as m_open,
        ):
            images, audio = await CobaltService.download_slideshow(res)

            self.assertIsNotNone(images)
            if images:
                self.assertEqual(len(images), 2)
                self.assertTrue(images[0].endswith("000.jpg"))
                self.assertTrue(images[1].endswith("001.jpg"))

            self.assertIsNotNone(audio)
            if audio:
                self.assertTrue(audio.endswith("audio.mp3"))
