import unittest
import os
import sys
from unittest.mock import MagicMock, patch

# Mock yt_dlp before importing app
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.service import YtDlpService
from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError
)

class TestYtDlpServiceListFormats(unittest.TestCase):
    def setUp(self):
        # Setup mocks before YtDlpService init
        self.service = YtDlpService()
        # Mock cookies path and aria2 presence for deterministic testing
        self.service.cookies_manager = MagicMock()
        self.service.cookies_manager.cookies_path = "/tmp/cookies.txt"
        self.service.has_aria2 = True

    def test_happy_path(self):
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
                    "protocol": "https"
                },
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "filesize": 10 * 1024 * 1024,
                    "protocol": "https"
                }
            ]
        }

        with patch.object(self.service, 'extract', return_value=mock_info) as mock_extract:
            title, formats, special_format, duration = self.service.list_formats("http://example.com/video")

            self.assertEqual(title, "Test Video")
            self.assertEqual(duration, "02:00")
            # Should filter out audio-only format "140" because it has no height and vcodec=none logic in parser
            # But wait, parser logic: if vcodec="none" and not tiktok -> skip.
            # My mock data didn't specify vcodec, so let's see.
            # In parse_format_metadata: if format_dict.get("vcodec") == "none" -> return None
            # If vcodec is missing, it passes.
            # However, height is missing for 140.
            # In parse_format_metadata: if not height -> try extracting from note -> if not found -> return None (unless tiktok).
            # So 140 should be skipped because it has no height.

            # Format 137 has height 1080.
            self.assertEqual(len(formats), 1)
            self.assertEqual(formats[0].format_id, "137")
            self.assertIn("📺 1080p", formats[0].label)

            self.assertEqual(special_format.format_id, "bestaudio/best")

    def test_live_stream_exception(self):
        """Test that live streams raise a specific exception."""
        mock_info = {
            "is_live": True,
            "title": "Live Stream"
        }
        with patch.object(self.service, 'extract', return_value=mock_info):
            with self.assertRaises(LiveStreamError) as cm:
                self.service.list_formats("http://example.com/live")
            self.assertIn("прямая трансляция", str(cm.exception))

    def test_access_denied_exception(self):
        """Test that 403 Forbidden raises a user-friendly exception."""
        with patch.object(self.service, 'extract', side_effect=Exception("HTTP Error 403: Forbidden")):
            with self.assertRaises(AccessDeniedError) as cm:
                self.service.list_formats("http://example.com/private")
            self.assertIn("Доступ запрещен", str(cm.exception))

    def test_not_found_exception(self):
        """Test that 404 Not Found raises a user-friendly exception."""
        with patch.object(self.service, 'extract', side_effect=Exception("HTTP Error 404: Not Found")):
            with self.assertRaises(VideoNotFoundError) as cm:
                self.service.list_formats("http://example.com/missing")
            self.assertIn("Видео не найдено", str(cm.exception))

    def test_generic_extraction_error(self):
        """Test that generic errors are wrapped."""
        with patch.object(self.service, 'extract', side_effect=Exception("Some random error")):
            with self.assertRaises(ExtractionError) as cm:
                self.service.list_formats("http://example.com/error")
            self.assertIn("Ошибка извлечения", str(cm.exception))
            self.assertIn("Some random error", str(cm.exception))

    def test_youtube_subprocess_fallback_on_error(self):
        """Test fallback to subprocess when extract fails for YouTube."""
        url = "https://youtube.com/watch?v=123"
        mock_info = {
            "title": "Fallback Video",
            "duration": 60,
            "formats": [
                {"format_id": "22", "ext": "mp4", "height": 720, "filesize": 50000}
            ]
        }

        # Mock extract to fail, and _extract_youtube_via_subprocess to succeed
        with patch.object(self.service, 'extract', side_effect=Exception("API Error")):
            with patch.object(self.service, '_extract_youtube_via_subprocess', return_value=mock_info) as mock_subprocess:
                title, formats, _, _ = self.service.list_formats(url)

                mock_subprocess.assert_called_once_with(url)
                self.assertEqual(title, "Fallback Video")
                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "22")

    def test_youtube_subprocess_fallback_on_empty_formats(self):
        """Test fallback to subprocess when extract returns no formats for YouTube."""
        url = "https://youtube.com/watch?v=123"
        mock_info_initial = {
            "title": "Empty Formats",
            "duration": 60,
            "formats": []
        }
        mock_info_subprocess = {
            "title": "Subprocess Video",
            "duration": 60,
            "formats": [
                {"format_id": "18", "ext": "mp4", "height": 360, "filesize": 20000}
            ]
        }

        with patch.object(self.service, 'extract', return_value=mock_info_initial):
            with patch.object(self.service, '_extract_youtube_via_subprocess', return_value=mock_info_subprocess) as mock_subprocess:
                # Mock parse_format_metadata to ensure the subprocess format is accepted
                # Actually, real parser works fine for simple dicts

                title, formats, _, _ = self.service.list_formats(url)

                # Logic: extract -> empty formats -> check if is_youtube and not used_subprocess -> call subprocess
                mock_subprocess.assert_called_once_with(url)

                # Note: list_formats uses title from initial info if present, or subprocess info?
                # Code: title = info.get("title") or "Видео"
                # It uses 'info' which came from extract(). So title should be "Empty Formats"
                self.assertEqual(title, "Empty Formats")

                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "18")

    def test_youtube_subprocess_fallback_on_filtered_formats(self):
        """Test fallback to subprocess when all formats are filtered out."""
        url = "https://youtube.com/watch?v=123"
        # Initial formats that will be filtered (e.g., unsupported ext)
        mock_info_initial = {
            "title": "Filtered Formats",
            "duration": 60,
            "formats": [
                {"format_id": "bad", "ext": "xyz", "height": 720}
            ]
        }
        # Subprocess formats that are valid
        mock_info_subprocess = {
            "title": "Subprocess Video",
            "duration": 60,
            "formats": [
                {"format_id": "good", "ext": "mp4", "height": 720, "filesize": 50000}
            ]
        }

        with patch.object(self.service, 'extract', return_value=mock_info_initial):
            with patch.object(self.service, '_extract_youtube_via_subprocess', return_value=mock_info_subprocess) as mock_subprocess:

                title, formats, _, _ = self.service.list_formats(url)

                mock_subprocess.assert_called_once_with(url)
                self.assertEqual(title, "Filtered Formats")
                self.assertEqual(len(formats), 1)
                self.assertEqual(formats[0].format_id, "good")

    def test_tiktok_is_passed_to_parsers(self):
        """Test that TikTok URLs trigger correct flags in parsers."""
        url = "https://tiktok.com/@user/video/123"
        mock_info = {
            "title": "TikTok Video",
            "duration": 15,
            "formats": [
                {"format_id": "tt", "ext": "mp4", "height": 720, "filesize": 10000}
            ]
        }

        with patch.object(self.service, 'extract', return_value=mock_info):
            with patch('app.services.ytdlp.service.parse_format_metadata') as mock_parse:
                # Setup return value so flow continues
                mock_parse.return_value = MagicMock(height=720, filesize=10000, format_id="tt", ext="mp4")

                self.service.list_formats(url)

                # Check calls to parse_format_metadata
                # Args: (format_dict, duration, is_tiktok)
                args = mock_parse.call_args[0]
                self.assertTrue(args[2], "is_tiktok should be True for TikTok URL")

    def test_max_items_limit(self):
        """Test that the number of returned formats respects max_items."""
        url = "http://example.com/video"
        # Generate 20 valid formats with different heights to pass deduplication
        formats = []
        for i in range(20):
            formats.append({
                "format_id": f"fmt_{i}",
                "ext": "mp4",
                "height": 100 + i, # Different height for each
                "filesize": 1000 * (i+1),
                "protocol": "https"
            })

        mock_info = {
            "title": "Many Formats",
            "duration": 100,
            "formats": formats
        }

        with patch.object(self.service, 'extract', return_value=mock_info):
            # Pass max_items=5
            _, formats_list, _, _ = self.service.list_formats(url, max_items=5)
            self.assertEqual(len(formats_list), 5)
            # Should return the top 5 (highest height/size)
            # Since we appended 100+i, the last ones are the biggest.
            # Sorting is reverse=True, so biggest first.
            self.assertEqual(formats_list[0].format_id, "fmt_19")
            self.assertEqual(formats_list[4].format_id, "fmt_15")

    def test_formats_sorting(self):
        """Test that formats are sorted by height and filesize descending."""
        url = "http://example.com/video"
        formats = [
            {"format_id": "low_res", "ext": "mp4", "height": 360, "filesize": 100},
            {"format_id": "high_res_small", "ext": "mp4", "height": 1080, "filesize": 500},
            {"format_id": "high_res_big", "ext": "mp4", "height": 1080, "filesize": 1000},
            {"format_id": "med_res", "ext": "mp4", "height": 720, "filesize": 300},
        ]

        mock_info = {
            "title": "Sort Test",
            "duration": 100,
            "formats": formats
        }

        with patch.object(self.service, 'extract', return_value=mock_info):
            _, formats_list, _, _ = self.service.list_formats(url)

            # Expected behavior:
            # Sort order before deduplication:
            # 1. high_res_big (1080p, 1000)
            # 2. high_res_small (1080p, 500)
            # 3. med_res (720p, 300)
            # 4. low_res (360p, 100)

            # Deduplication keeps the first format of each height:
            # 1. high_res_big (kept)
            # 2. high_res_small (dropped - duplicate 1080p)
            # 3. med_res (kept)
            # 4. low_res (kept)

            self.assertEqual(len(formats_list), 3)
            self.assertEqual(formats_list[0].format_id, "high_res_big")
            self.assertEqual(formats_list[1].format_id, "med_res")
            self.assertEqual(formats_list[2].format_id, "low_res")

if __name__ == '__main__':
    unittest.main()
