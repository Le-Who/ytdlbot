import unittest
import os
import sys

# Add repo root to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.ytdlp_service import YtDlpService
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
            page_url="http://example.com/video",
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
        self.assertIn("http://example.com/video", cmd)
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
            page_url="http://example.com/song",
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

if __name__ == '__main__':
    unittest.main()
