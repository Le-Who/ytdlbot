import unittest
import os
import sys
from unittest.mock import MagicMock, patch

# Mock yt_dlp before importing app
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.service import YtDlpService
from app.services.ytdlp.models import FormatItem, FormatMetadata
from app.constants import GIF_FORMAT_ID

class TestYtDlpService(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()
        # Mock cookies path and aria2 presence for deterministic testing
        # We can't easily mock inner attributes if they are properties or hidden,
        # but cookies_path is a property reading from cookies_manager.
        # So we mock cookies_manager.cookies_path
        self.service.cookies_manager.cookies_path = "/tmp/cookies.txt"
        self.service.has_aria2 = True

    def test_build_command_video(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/123",
            format_id="137+140",
            height=1080,
            output="/tmp/out.mp4"
        )
        self.assertIn("yt-dlp", cmd)
        # Check format string construction
        # expected format contains: bestvideo[height=1080]+(bestaudio[format_note*=original]/bestaudio[language^=en]/bestaudio[language^=orig]/bestaudio)/best[height=1080]/best
        # We check substring
        self.assertTrue(any("bestvideo[height=1080]" in arg for arg in cmd))
        self.assertIn("/tmp/out.mp4", cmd)
        self.assertIn("https://www.tiktok.com/@user/video/123", cmd)
        self.assertIn("--cookies", cmd)
        self.assertIn("/tmp/cookies.txt", cmd)

    def test_build_command_gif(self):
        cmd = self.service.build_command(
            page_url="http://pinterest.com/pin/123",
            format_id=GIF_FORMAT_ID,
            height=None,
            output="/tmp/out.mp4"
        )
        # Should select video only for GIF conversion
        expected_fmt = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"
        self.assertIn(expected_fmt, cmd)

    def test_build_command_audio(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/456",
            format_id="bestaudio/best",
            height=None,
            output="/tmp/out.mp3"
        )
        self.assertIn("bestaudio/best", cmd)
        self.assertIn("--cookies", cmd)

    def test_build_command_aria2(self):
        # Case 1: Stream to pipe (aria2 disabled)
        cmd = self.service.build_command(
            page_url="http://example.com/video",
            format_id="best",
            height=720,
            output="-",
            use_aria2=True
        )
        # Check that aria2c is NOT in the arguments
        self.assertFalse(any("aria2c" in arg for arg in cmd))

        # Case 2: Download to file (aria2 enabled)
        cmd = self.service.build_command(
            page_url="http://example.com/video",
            format_id="best",
            height=720,
            output="/tmp/file.mp4",
            use_aria2=True
        )
        self.assertTrue(any("aria2c" in arg for arg in cmd))

    def test_list_formats_passes_is_tiktok(self):
        mock_info = {
            "title": "Test Video",
            "duration": 60,
            "formats": [
                {
                    "format_id": "1",
                    "ext": "mp4",
                    "height": 720,
                    "filesize": 1000,
                    "protocol": "https",
                    "vcodec": "h264"
                }
            ]
        }

        with patch.object(self.service, 'extract', return_value=mock_info):
            with patch('app.services.ytdlp.service.parse_format_metadata') as mock_parse:
                mock_parse.return_value = FormatMetadata("1", "mp4", 720, 1000, "https")

                with patch('app.services.ytdlp.service.deduplicate_formats') as mock_dedup:
                    mock_dedup.return_value = [FormatMetadata("1", "mp4", 720, 1000, "https")]

                    with patch('app.services.ytdlp.service.create_format_item') as mock_create:
                        mock_create.return_value = FormatItem("1", "Label", "mp4", 720, 1000)

                        # Test YouTube
                        url = "https://youtube.com/watch?v=123"
                        self.service.list_formats(url)

                        # Verify parse_format_metadata arg
                        args_parse = mock_parse.call_args[0]
                        self.assertFalse(args_parse[2], "parse_format: is_tiktok should be False for YouTube")

                        # Verify deduplicate_formats arg
                        args_dedup = mock_dedup.call_args[0]
                        self.assertFalse(args_dedup[1], "deduplicate_formats: is_tiktok should be False for YouTube")

                        # Test TikTok
                        url_tiktok = "https://tiktok.com/@user/video/123"
                        self.service.list_formats(url_tiktok)

                        # Verify parse_format_metadata arg
                        args_parse = mock_parse.call_args[0]
                        self.assertTrue(args_parse[2], "parse_format: is_tiktok should be True for TikTok")

                        # Verify deduplicate_formats arg
                        args_dedup = mock_dedup.call_args[0]
                        self.assertTrue(args_dedup[1], "deduplicate_formats: is_tiktok should be True for TikTok")

if __name__ == '__main__':
    unittest.main()
