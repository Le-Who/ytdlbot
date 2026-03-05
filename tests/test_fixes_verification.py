
import unittest
import os

# Add repo root to path
# Set dummy env vars for app.main import
os.environ["BOT_TOKEN"] = "123456:ABC-DEF"
os.environ["BASE_URL"] = "http://localhost:8000"

from app.services.ytdlp.service import YtDlpService
# from app.main import build_format_keyboard # Remove for now if not used

class TestFixesVerification(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()

    def test_robust_format_selection(self):
        # Verify that build_command now includes the new fallbacks and flags
        cmd = self.service.build_command(
            page_url="https://youtube.com/watch?v=123",
            format_id="137",
            height=1080,
            output="out.mp4"
        )
        
        # Check for cascading fallback in format selector
        # We expect: 137+(audio_sel)/best[height=1080]/bestvideo+bestaudio/best
        # wait, the code does: f"{video_sel}+({audio_sel})/{prog_sel}/bestvideo+bestaudio/best"
        self.assertTrue(any("bestvideo+bestaudio/best" in arg for arg in cmd))
        self.assertNotIn("--no-check-certificate", cmd)
        
    def test_audio_selector_robustness(self):
        cmd = self.service.build_command(
            page_url="https://youtube.com/watch?v=123",
            format_id="137",
            height=1080,
            output="out.mp4"
        )
        # Find the --format argument
        fmt_arg = None
        for i, arg in enumerate(cmd):
            if arg == "--format":
                fmt_arg = cmd[i+1]
                break
        
        self.assertIsNotNone(fmt_arg)
        self.assertIn("bestaudio[ext=m4a]", fmt_arg)

if __name__ == '__main__':
    unittest.main()
