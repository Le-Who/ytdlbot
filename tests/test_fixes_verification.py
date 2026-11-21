import os
import unittest

os.environ["BOT_TOKEN"] = "123456:ABC-DEF"
os.environ["BASE_URL"] = "http://localhost:8000"

from app.services.ytdlp.service import YtDlpService


class TestFixesVerification(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()

    def test_robust_format_selection(self):
        cmd = self.service.build_command(
            page_url="https://youtube.com/watch?v=123",
            format_id="137",
            height=1080,
            output="out.mp4",
        )

        fmt_arg = cmd[cmd.index("--format") + 1]
        branches = fmt_arg.split("/")
        self.assertTrue(
            all(
                "[height<=1080]" in branch or "[width<=1080]" in branch
                for branch in branches
            )
        )
        self.assertTrue(
            all(
                "bestaudio" in branch or "[acodec!=none]" in branch
                for branch in branches
            )
        )
        self.assertNotIn("bestvideo+bestaudio/best", fmt_arg)
        self.assertNotIn("--no-check-certificate", cmd)

    def test_audio_selector_robustness(self):
        cmd = self.service.build_command(
            page_url="https://youtube.com/watch?v=123",
            format_id="137",
            height=1080,
            output="out.mp4",
        )
        fmt_arg = None
        for i, arg in enumerate(cmd):
            if arg == "--format":
                fmt_arg = cmd[i + 1]
                break

        self.assertIsNotNone(fmt_arg)
        self.assertIn("bestvideo", fmt_arg)


if __name__ == "__main__":
    unittest.main()
