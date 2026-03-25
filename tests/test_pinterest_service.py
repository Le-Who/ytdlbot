"""Tests for Pinterest Native Service v2 (streaming + carousel)."""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestPinterestExtractMediaUrl(unittest.IsolatedAsyncioTestCase):
    """Test PinterestNativeService.extract_media_url."""

    async def test_extracts_og_video_url(self):
        from app.services.pinterest import PinterestNativeService

        html = (
            "<html><head>"
            '<meta property="og:video" content="https://v1.pinimg.com/videos/mc/720p/ab/cd.mp4">'
            "</head></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            video_url, image_url = await PinterestNativeService.extract_media_url(
                "https://pinterest.com/pin/123"
            )

        self.assertEqual(video_url, "https://v1.pinimg.com/videos/mc/720p/ab/cd.mp4")

    async def test_extracts_og_image_when_no_video(self):
        from app.services.pinterest import PinterestNativeService

        html = (
            "<html><head>"
            '<meta property="og:image" content="https://i.pinimg.com/originals/ab/cd/ef.jpg">'
            "</head></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            video_url, image_url = await PinterestNativeService.extract_media_url(
                "https://pinterest.com/pin/456"
            )

        self.assertIsNone(video_url)
        self.assertEqual(image_url, "https://i.pinimg.com/originals/ab/cd/ef.jpg")

    async def test_returns_none_on_http_error(self):
        from app.services.pinterest import PinterestNativeService

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            video_url, image_url = await PinterestNativeService.extract_media_url(
                "https://pinterest.com/pin/999"
            )

        self.assertIsNone(video_url)
        self.assertIsNone(image_url)

    async def test_returns_none_on_network_error(self):
        from app.services.pinterest import PinterestNativeService

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(side_effect=ConnectionError("timeout"))

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            video_url, image_url = await PinterestNativeService.extract_media_url(
                "https://pinterest.com/pin/bad"
            )

        self.assertIsNone(video_url)
        self.assertIsNone(image_url)


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestPinterestOgParsers(unittest.TestCase):
    """Test private og:video / og:image regex parsers."""

    def test_extract_og_video_standard(self):
        from app.services.pinterest import _extract_og_video

        html = (
            '<meta property="og:video:secure_url" content="https://v.pinimg.com/v.mp4">'
        )
        self.assertEqual(_extract_og_video(html), "https://v.pinimg.com/v.mp4")

    def test_extract_og_video_reversed_attrs(self):
        from app.services.pinterest import _extract_og_video

        html = '<meta content="https://v.pinimg.com/v2.mp4" property="og:video">'
        self.assertEqual(_extract_og_video(html), "https://v.pinimg.com/v2.mp4")

    def test_extract_og_video_no_match(self):
        from app.services.pinterest import _extract_og_video

        html = '<meta property="og:title" content="A Pin">'
        self.assertIsNone(_extract_og_video(html))

    def test_extract_og_video_rejects_http(self):
        from app.services.pinterest import _extract_og_video

        html = '<meta property="og:video" content="http://insecure.com/v.mp4">'
        self.assertIsNone(_extract_og_video(html))

    def test_extract_og_image(self):
        from app.services.pinterest import _extract_og_image

        html = '<meta property="og:image" content="https://i.pinimg.com/img.jpg">'
        self.assertEqual(_extract_og_image(html), "https://i.pinimg.com/img.jpg")

    def test_extract_og_image_none_when_missing(self):
        from app.services.pinterest import _extract_og_image

        html = "<html><head></head></html>"
        self.assertIsNone(_extract_og_image(html))


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestPinterestStreamDownload(unittest.IsolatedAsyncioTestCase):
    """Test _stream_download helper."""

    async def test_stream_download_writes_chunks(self):
        from app.services.pinterest import _stream_download

        tmpdir = tempfile.mkdtemp()
        chunks = [b"chunk1", b"chunk2", b"chunk3"]

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _aiter():
            for c in chunks:
                yield c

        mock_resp.aiter_content = _aiter

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            with patch("app.services.pinterest.TEMP_DIR", tmpdir):
                result = await _stream_download(
                    "https://example.com/video.mp4", "test_", "mp4"
                )

        self.assertIsNotNone(result)
        self.assertTrue(os.path.exists(result))
        with open(result, "rb") as f:
            data = f.read()
        self.assertEqual(data, b"chunk1chunk2chunk3")
        os.unlink(result)
        os.rmdir(tmpdir)

    async def test_stream_download_returns_none_on_http_error(self):
        from app.services.pinterest import _stream_download

        mock_resp = MagicMock()
        mock_resp.status_code = 403

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            result = await _stream_download(
                "https://example.com/denied.mp4", "test_err_", "mp4"
            )

        self.assertIsNone(result)

    async def test_stream_download_returns_none_on_empty_response(self):
        from app.services.pinterest import _stream_download

        tmpdir = tempfile.mkdtemp()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _aiter():
            return
            yield  # makes it async generator  # noqa

        mock_resp.aiter_content = _aiter

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.pinterest.AsyncSession", return_value=mock_session):
            with patch("app.services.pinterest.TEMP_DIR", tmpdir):
                result = await _stream_download(
                    "https://example.com/empty.mp4", "test_empty_", "mp4"
                )

        self.assertIsNone(result)
        os.rmdir(tmpdir)


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestPinterestDownloadVideo(unittest.IsolatedAsyncioTestCase):
    """Test PinterestNativeService.download_video full pipeline."""

    async def test_tier1_video_download(self):
        from app.services.pinterest import PinterestNativeService

        with patch.object(
            PinterestNativeService,
            "extract_media_url",
            new_callable=AsyncMock,
            return_value=("https://v.pinimg.com/video.mp4", None),
        ):
            with patch(
                "app.services.pinterest._stream_download",
                new_callable=AsyncMock,
                return_value="/tmp/pin_test.mp4",
            ) as mock_dl:
                path, error = await PinterestNativeService.download_video(
                    "https://pinterest.com/pin/123"
                )

        self.assertEqual(path, "/tmp/pin_test.mp4")
        self.assertIsNone(error)
        mock_dl.assert_awaited_once_with(
            "https://v.pinimg.com/video.mp4", "pin_", "mp4"
        )

    async def test_tier2_image_fallback(self):
        from app.services.pinterest import PinterestNativeService

        with patch.object(
            PinterestNativeService,
            "extract_media_url",
            new_callable=AsyncMock,
            return_value=(None, "https://i.pinimg.com/img.jpg"),
        ):
            with patch(
                "app.services.pinterest._stream_download",
                new_callable=AsyncMock,
                return_value="/tmp/pin_img.jpg",
            ):
                path, error = await PinterestNativeService.download_video(
                    "https://pinterest.com/pin/456"
                )

        self.assertEqual(path, "/tmp/pin_img.jpg")
        self.assertIsNone(error)

    async def test_tier3_cobalt_fallback(self):
        from app.services.pinterest import PinterestNativeService
        from app.services.cobalt import CobaltResult

        with patch.object(
            PinterestNativeService,
            "extract_media_url",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            with patch(
                "app.services.cobalt.CobaltService.process",
                new_callable=AsyncMock,
                return_value=CobaltResult(
                    status="tunnel",
                    url="https://cobalt.example.com/video.mp4",
                ),
            ):
                with patch(
                    "app.services.pinterest._stream_download",
                    new_callable=AsyncMock,
                    return_value="/tmp/pin_cobalt.mp4",
                ):
                    path, error = await PinterestNativeService.download_video(
                        "https://pinterest.com/pin/789"
                    )

        self.assertEqual(path, "/tmp/pin_cobalt.mp4")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
