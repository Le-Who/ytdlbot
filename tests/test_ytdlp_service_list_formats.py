from unittest.mock import AsyncMock
import unittest
import sys
from unittest.mock import MagicMock, patch

# Mock yt_dlp before importing app
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
from app.services.ytdlp.service import YtDlpService
from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
)


class TestYtDlpServiceListFormats(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Setup mocks before YtDlpService init
        self.service = YtDlpService()
        # Mock cookies path for deterministic testing
        self.service.cookies_manager = MagicMock()
        self.service.cookies_manager.cookies_path = "/tmp/cookies.txt"

    async def test_happy_path(self):
        """Test successful format extraction."""
        mock_info = {
            "title": "Test Video",
            "duration": 120,
            "formats": [
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "height": 1080,
                    "filesize": 100 * 1024 * 1024,
                    "protocol": "https",
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "filesize": 10 * 1024 * 1024,
                    "protocol": "https",
                },
            ],
        }

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info
            result = await self.service.list_formats("http://example.com/video")
            title = result.title
            formats = result.formats
            special_format = result.special_format
            duration = result.duration_str

            self.assertEqual(title, "Test Video")
            self.assertEqual(duration, "02:00")
            
            self.assertEqual(len(formats), 1)
            self.assertEqual(formats[0].format_id, "137")
            self.assertEqual(formats[0].height, 1080)

            self.assertEqual(special_format.format_id, "bestaudio/best")

    async def test_live_stream_exception(self):
        """Test that live streams raise a specific exception."""
        mock_info = {"is_live": True, "title": "Live Stream"}
        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info
            with self.assertRaises(LiveStreamError) as cm:
                await self.service.list_formats("http://example.com/live")
            self.assertIn("прямая трансляция", str(cm.exception))

    async def test_access_denied_exception(self):
        """Test that 403 Forbidden raises a user-friendly exception."""
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("HTTP Error 403: Forbidden")
            with self.assertRaises(AccessDeniedError) as cm:
                await self.service.list_formats("http://example.com/private")
            self.assertIn("Доступ запрещен", str(cm.exception))

    async def test_not_found_exception(self):
        """Test that 404 Not Found raises a user-friendly exception."""
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("HTTP Error 404: Not Found")
            with self.assertRaises(VideoNotFoundError) as cm:
                await self.service.list_formats("http://example.com/missing")
            self.assertIn("Видео не найдено", str(cm.exception))

    async def test_generic_extraction_error(self):
        """Test that generic errors are wrapped."""
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("Some random error")
            with self.assertRaises(ExtractionError) as cm:
                await self.service.list_formats("http://example.com/error")
            self.assertIn("Ошибка извлечения", str(cm.exception))
            self.assertIn("Some random error", str(cm.exception))

    async def test_youtube_subprocess_fallback_on_error(self):
        """Test fallback to subprocess when extract fails for YouTube."""
        url = "https://youtube.com/watch?v=123"
        mock_info = {
            "title": "Fallback Video",
            "duration": 60,
            "formats": [
                {"format_id": "22", "ext": "mp4", "height": 720, "filesize": 50000}
            ],
        }

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.side_effect = Exception("API Error")
            with patch.object(
                self.service, "_extract_youtube_via_subprocess", new_callable=AsyncMock
            ) as mock_subprocess:
                mock_subprocess.return_value = mock_info
                result = await self.service.list_formats(url)
                title = result.title
                formats = result.formats

                mock_subprocess.assert_called_once_with(url)
                self.assertEqual(title, "Fallback Video")
                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "22")

    async def test_youtube_subprocess_fallback_on_empty_formats(self):
        """Test fallback to subprocess when extract returns no formats for YouTube."""
        url = "https://youtube.com/watch?v=123"
        mock_info_initial = {"title": "Empty Formats", "duration": 60, "formats": []}
        mock_info_subprocess = {
            "title": "Subprocess Video",
            "duration": 60,
            "formats": [
                {"format_id": "18", "ext": "mp4", "height": 360, "filesize": 20000}
            ],
        }

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info_initial
            with patch.object(
                self.service,
                "_extract_youtube_via_subprocess",
                new_callable=AsyncMock
            ) as mock_subprocess:
                mock_subprocess.return_value = mock_info_subprocess

                result = await self.service.list_formats(url)
                title = result.title
                formats = result.formats

                mock_subprocess.assert_called_once_with(url)
                self.assertEqual(title, "Empty Formats")

                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "18")

    async def test_youtube_subprocess_fallback_on_filtered_formats(self):
        """Test fallback to subprocess when all formats are filtered out."""
        url = "https://youtube.com/watch?v=123"
        mock_info_initial = {
            "title": "Filtered Formats",
            "duration": 60,
            "formats": [{"format_id": "bad", "ext": "xyz", "height": 720}],
        }
        mock_info_subprocess = {
            "title": "Subprocess Video",
            "duration": 60,
            "formats": [
                {"format_id": "good", "ext": "mp4", "height": 720, "filesize": 50000}
            ],
        }

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info_initial
            with patch.object(
                self.service,
                "_extract_youtube_via_subprocess",
                new_callable=AsyncMock
            ) as mock_subprocess:
                mock_subprocess.return_value = mock_info_subprocess
                result = await self.service.list_formats(url)
                title = result.title
                formats = result.formats

                mock_subprocess.assert_called_once_with(url)
                self.assertEqual(title, "Filtered Formats")
                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "good")

    async def test_tiktok_is_passed_to_parsers(self):
        """Test that TikTok URLs trigger correct flags in parsers."""
        url = "https://tiktok.com/@user/video/123"
        mock_info = {
            "title": "TikTok Video",
            "duration": 15,
            "formats": [
                {"format_id": "tt", "ext": "mp4", "height": 720, "filesize": 10000}
            ],
        }

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info
            with patch(
                "app.services.ytdlp.service.parse_format_metadata"
            ) as mock_parse:
                mock_parse.return_value = MagicMock(
                    height=720, filesize=10000, format_id="tt", ext="mp4"
                )

                await self.service.list_formats(url)

                args = mock_parse.call_args[0]
                self.assertTrue(args[2], "is_tiktok should be True for TikTok URL")

    async def test_max_items_limit(self):
        """Test that the number of returned formats respects max_items."""
        url = "http://example.com/video"
        formats = []
        for i in range(20):
            formats.append(
                {
                    "format_id": f"fmt_{i}",
                    "ext": "mp4",
                    "height": 100 + i,
                    "filesize": 1000 * (i + 1),
                    "protocol": "https",
                }
            )

        mock_info = {"title": "Many Formats", "duration": 100, "formats": formats}

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info
            result = await self.service.list_formats(url, max_items=5)
            formats_list = result.formats
            self.assertEqual(len(formats_list), 5)
            self.assertEqual(formats_list[0].format_id, "fmt_19")
            self.assertEqual(formats_list[4].format_id, "fmt_15")

    async def test_formats_sorting(self):
        """Test that formats are sorted by height and filesize descending."""
        url = "http://example.com/video"
        formats = [
            {"format_id": "low_res", "ext": "mp4", "height": 360, "filesize": 100},
            {
                "format_id": "high_res_small",
                "ext": "mp4",
                "height": 1080,
                "filesize": 500,
            },
            {
                "format_id": "high_res_big",
                "ext": "mp4",
                "height": 1080,
                "filesize": 1000,
            },
            {"format_id": "med_res", "ext": "mp4", "height": 720, "filesize": 300},
        ]

        mock_info = {"title": "Sort Test", "duration": 100, "formats": formats}

        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.return_value = mock_info
            result = await self.service.list_formats(url)
            formats_list = result.formats

            self.assertEqual(len(formats_list), 3)
            self.assertEqual(formats_list[0].format_id, "high_res_big")
            self.assertEqual(formats_list[1].format_id, "med_res")
            self.assertEqual(formats_list[2].format_id, "low_res")

    async def test_tiktok_photo_url_returns_slideshow(self):
        """Test that TikTok /photo/ URLs that fail yt-dlp return is_slideshow=True."""
        url = "https://www.tiktok.com/@user/photo/7611488001083886868"

        with patch.object(
            self.service,
            "extract",
            new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("ERROR: Unsupported URL: " + url)
            result = await self.service.list_formats(url)
            title = result.title
            formats = result.formats
            duration = result.duration_str
            is_slideshow = result.is_slideshow

            self.assertTrue(
                is_slideshow, "TikTok /photo/ URL should be detected as slideshow"
            )
            self.assertEqual(title, "TikTok Slideshow")
            self.assertEqual(formats, [])
            self.assertEqual(duration, "—")

    async def test_tiktok_unsupported_url_returns_slideshow(self):
        """Any TikTok URL that yt-dlp can't handle should fallback to slideshow."""
        url = "https://tiktok.com/@creator/photo/123456789"

        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("Unsupported URL")
            result = await self.service.list_formats(url)
            is_slideshow = result.is_slideshow
            self.assertTrue(is_slideshow)

    async def test_non_tiktok_unsupported_url_raises_error(self):
        """Non-TikTok unsupported URLs should still raise ExtractionError."""
        url = "https://example.com/video/123"

        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("Unsupported URL")
            with self.assertRaises(ExtractionError):
                await self.service.list_formats(url)

    async def test_tiktok_auth_error_returns_tikwm_fallback(self):
        """TikTok auth errors: return tikwm_fallback if no proxy is configured."""
        url = "https://tiktok.com/@user/video/123"
        error_msg = (
            "ERROR: [TikTok] 123: This post may not be comfortable. "
            "Log in for access. Use --cookies-from-browser or --cookies"
        )
        self.service.tiktok_proxy = None  # no proxy
        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.side_effect = Exception(error_msg)
            result = await self.service.list_formats(url)
            title = result.title
            formats = result.formats
            is_slideshow = result.is_slideshow
            self.assertEqual(title, "TikTok Video")
            self.assertFalse(is_slideshow)

            self.assertEqual(len(formats), 1)
            self.assertEqual(formats[0].format_id, "tikwm_fallback")

    async def test_tiktok_sign_in_error_returns_tikwm_fallback(self):
        """TikTok 'sign in' errors: return tikwm_fallback if no proxy."""
        url = "https://tiktok.com/@user/video/456"
        self.service.tiktok_proxy = None
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("Sign in to confirm")
            result = await self.service.list_formats(url)
            formats = result.formats
            self.assertEqual(formats[0].format_id, "tikwm_fallback")

    async def test_tiktok_auth_error_with_proxy_returns_gallerydl_fallback(self):
        """When proxy configured, auth error returns both gallerydl_fallback and tikwm_fallback."""
        url = "https://tiktok.com/@user/video/789"
        error_msg = "This post may not be comfortable. Log in for access"
        self.service.tiktok_proxy = "socks5://proxy:1080"  # enable gallery-dl path
        with patch.object(self.service, "extract", new_callable=AsyncMock) as mock_ext:
            mock_ext.side_effect = Exception(error_msg)
            result = await self.service.list_formats(url)
            formats = result.formats
            self.assertEqual(len(formats), 2)
            format_ids = [f.format_id for f in formats]
            self.assertIn("gallerydl_fallback", format_ids)
            self.assertIn("tikwm_fallback", format_ids)
        self.service.tiktok_proxy = None  # reset

    async def test_tiktok_unknown_error_falls_back_to_slideshow(self):
        """TikTok unknown errors (not auth, not unsupported) still try slideshow."""
        url = "https://tiktok.com/@user/video/789"
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_ext:
            mock_ext.side_effect = Exception("Some weird TikTok error")
            result = await self.service.list_formats(url)
            is_slideshow = result.is_slideshow
            self.assertTrue(is_slideshow)

if __name__ == "__main__":
    unittest.main()
