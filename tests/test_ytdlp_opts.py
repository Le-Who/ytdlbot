import unittest
import sys
from unittest.mock import MagicMock

# Mock yt_dlp before importing app code
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
from app.services.ytdlp.service import YtDlpService

class TestYtDlpOpts(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()
        self.service.cookies_manager.cookies_path = "/tmp/cookies.txt"

    def test_base_opts_structure(self):
        """Verify _base_opts returns expected keys."""
        opts = self.service._base_opts(for_list_formats=False)
        self.assertIn("quiet", opts)
        self.assertIn("socket_timeout", opts)
        self.assertIn("format", opts)
        self.assertIn("format_sort", opts)
        self.assertEqual(opts["socket_timeout"], self.service.SOCKET_TIMEOUT)

    def test_base_opts_list_formats(self):
        """Verify _base_opts excludes format options when for_list_formats=True."""
        opts = self.service._base_opts(for_list_formats=True)
        self.assertNotIn("format", opts)
        self.assertNotIn("format_sort", opts)
        self.assertIn("quiet", opts)

    def test_base_opts_independence(self):
        """Verify modifying returned opts does not affect subsequent calls (template isolation)."""
        opts1 = self.service._base_opts(for_list_formats=False)

        # Verify initial state — extractor_args includes TikTok defaults
        self.assertIsInstance(opts1["extractor_args"], dict)
        self.assertIn("tiktok", opts1["extractor_args"])

        # Modify opts1
        opts1["new_key"] = "value"
        opts1["extractor_args"]["injected"] = ["val1", "val2"]

        # Verify opts2 is independent
        opts2 = self.service._base_opts(for_list_formats=False)
        self.assertNotIn("new_key", opts2)

        # Check if dict was modified in opts2
        # This will pass if each call creates a NEW dict.
        self.assertNotIn("injected", opts2["extractor_args"])
        # TikTok defaults should be present but not the injected key
        self.assertIn("tiktok", opts2["extractor_args"])

if __name__ == "__main__":
    unittest.main()
