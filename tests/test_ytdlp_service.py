import unittest
import sys
from unittest.mock import MagicMock, patch, AsyncMock

# Mock yt_dlp before importing app
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
from app.services.ytdlp.service import YtDlpService
from app.constants import GIF_FORMAT_ID


class TestYtDlpService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = YtDlpService()
        # Mock PlatformCookiesManager to return a deterministic cookies path
        self.service.cookies_manager.get_cookies_path = MagicMock(
            return_value="/tmp/cookies.txt"
        )
        self.service.cookies_manager._global_cookies_path = "/tmp/cookies.txt"

    def test_build_command_video(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/123",
            format_id="137+140",
            height=1080,
            output="/tmp/out.mp4",
        )
        self.assertTrue(any("yt-dlp" in arg or "yt_dlp" in arg for arg in cmd))
        # Check format string construction
        # We check substring
        self.assertTrue(any("137+140" in arg for arg in cmd))
        self.assertIn("/tmp/out.mp4", cmd)
        self.assertIn("--", cmd)
        self.assertIn("https://www.tiktok.com/@user/video/123", cmd)
        self.assertIn("--cookies", cmd)
        self.assertIn("/tmp/cookies.txt", cmd)

    def test_build_command_gif(self):
        cmd = self.service.build_command(
            page_url="http://pinterest.com/pin/123",
            format_id=GIF_FORMAT_ID,
            height=None,
            output="/tmp/out.mp4",
        )
        # Should select video only for GIF conversion
        expected_fmt = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"
        self.assertIn(expected_fmt, cmd)

    def test_build_command_audio(self):
        cmd = self.service.build_command(
            page_url="https://www.tiktok.com/@user/video/456",
            format_id="bestaudio/best",
            height=None,
            output="/tmp/out.mp3",
        )
        self.assertIn("bestaudio/best", cmd)
        self.assertIn("--cookies", cmd)

    def test_build_command_no_aria2(self):
        # Verify aria2c is never in the command args
        cmd = self.service.build_command(
            page_url="http://example.com/video",
            format_id="best",
            height=720,
            output="/tmp/file.mp4",
        )
        self.assertFalse(any("aria2c" in arg for arg in cmd))

    async def test_list_formats_bypasses_extractor(self):
        with patch.object(
            self.service, "extract", new_callable=AsyncMock
        ) as mock_extract:
            # Test TikTok
            url_tiktok = "https://tiktok.com/@user/video/123"
            res_tiktok = await self.service.list_formats(url_tiktok)
            mock_extract.assert_not_called()
            self.assertEqual(len(res_tiktok.formats), 1)
            self.assertEqual(res_tiktok.formats[0].format_id, "tikwm_fallback")

            # Test Pinterest
            url_pin = "https://pinterest.com/pin/123"
            res_pin = await self.service.list_formats(url_pin)
            mock_extract.assert_not_called()
            self.assertEqual(len(res_pin.formats), 1)
            self.assertEqual(res_pin.formats[0].format_id, "pinterest_native")


if __name__ == "__main__":
    unittest.main()
