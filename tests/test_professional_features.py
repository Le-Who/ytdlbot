
import unittest
import os
import sys
from unittest.mock import MagicMock

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.ytdlp_service import YtDlpService

class TestProfessionalRefinements(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()
        self.service.has_aria2 = True

    def test_format_sort_in_opts(self):
        opts = self.service._base_opts()
        self.assertIn("format_sort", opts)
        self.assertEqual(opts["format_sort"], ["res:1080", "vcodec:vp9", "br", "size"])

    def test_concurrency_in_opts(self):
        opts = self.service._base_opts()
        self.assertEqual(opts.get("concurrent_fragment_downloads"), 5)

    def test_live_stream_rejection(self):
        # Mock extract to return a live stream info
        self.service.extract = MagicMock(return_value={"is_live": True, "title": "Live Video"})
        
        with self.assertRaises(Exception) as cm:
            self.service.list_formats("https://youtube.com/live/video")
        
        self.assertIn("прямая трансляция", str(cm.exception).lower())

    def test_aria2c_tuned_args(self):
        cmd = self.service.build_command("url", "best", 1080, "out", use_aria2=True)
        # Check if aria2c args are upgraded to -x 16
        self.assertIn("-x 16 -s 16 -k 1M", " ".join(cmd))
        self.assertIn("--concurrent-fragments", cmd)
        self.assertIn("5", cmd)
        self.assertIn("--no-playlist", cmd)

if __name__ == "__main__":
    unittest.main()
