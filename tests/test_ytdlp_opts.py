import unittest
import os
import sys
from unittest.mock import MagicMock, patch

# Mock yt_dlp before importing app code
sys.modules["yt_dlp"] = MagicMock()

# Add repo root to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.service import YtDlpService

class TestYtDlpOpts(unittest.TestCase):
    def setUp(self):
        # Mock shutil.which to avoid logging warning during init
        with patch("shutil.which", return_value="/usr/bin/aria2c"):
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

        # Verify initial state
        self.assertEqual(len(opts1["extractor_args"]["youtube"]["player_client"]), 4)

        # Modify opts1
        opts1["new_key"] = "value"
        opts1["extractor_args"]["youtube"]["player_client"].append("hacked")

        # Verify opts2 is independent
        opts2 = self.service._base_opts(for_list_formats=False)
        self.assertNotIn("new_key", opts2)

        # Check if list was modified in opts2
        # This will pass if each call creates a NEW list.
        # It will FAIL if they share the list.
        self.assertEqual(len(opts2["extractor_args"]["youtube"]["player_client"]), 4)
        self.assertNotEqual(opts1["extractor_args"]["youtube"]["player_client"], opts2["extractor_args"]["youtube"]["player_client"])

if __name__ == '__main__':
    unittest.main()
