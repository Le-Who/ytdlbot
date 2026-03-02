import unittest
import os
import sys

# Add repo root to path so we can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.parsers import _create_format_label

class TestUXLabels(unittest.TestCase):
    def test_high_res_label(self):
        # 1080p -> 📺 1080p • 100.0 MB
        label = _create_format_label(
            height=1080,
            filesize=100 * 1024 * 1024,
            protocol="https",
            is_tiktok=False
        )
        self.assertIn("📺", label)
        self.assertIn("1080p", label)
        self.assertIn("100.0 MB", label)
        self.assertIn("•", label)

    def test_hd_res_label(self):
        # 720p -> 📹 720p • 50.0 MB
        label = _create_format_label(
            height=720,
            filesize=50 * 1024 * 1024,
            protocol="https",
            is_tiktok=False
        )
        self.assertIn("📹", label)
        self.assertIn("720p", label)
        self.assertIn("50.0 MB", label)

    def test_low_res_label(self):
        # 480p -> 📱 480p • 10.0 MB
        label = _create_format_label(
            height=480,
            filesize=10 * 1024 * 1024,
            protocol="https",
            is_tiktok=False
        )
        self.assertIn("📱", label)
        self.assertIn("480p", label)
        self.assertIn("10.0 MB", label)

    def test_tiktok_label(self):
        # TikTok -> 🎵 TikTok
        label = _create_format_label(
            height=None,
            filesize=5 * 1024 * 1024,
            protocol="https",
            is_tiktok=True
        )
        self.assertIn("🎵", label)
        self.assertIn("TikTok", label)
        self.assertIn("5.0 MB", label)

    def test_kb_size(self):
        label = _create_format_label(
            height=360,
            filesize=500 * 1024, # 500 KB
            protocol="https",
            is_tiktok=False
        )
        self.assertIn("500 KB", label)

    def test_hls_stream(self):
        label = _create_format_label(
            height=720,
            filesize=None,
            protocol="m3u8_native",
            is_tiktok=False
        )
        self.assertIn("HLS", label)

    def test_unknown_size(self):
        label = _create_format_label(
            height=720,
            filesize=None,
            protocol="https",
            is_tiktok=False
        )
        # No size suffix when filesize is None and protocol is not HLS
        self.assertIn("720p", label)
        self.assertNotIn("MB", label)
        self.assertNotIn("KB", label)
        self.assertNotIn("HLS", label)

if __name__ == '__main__':
    unittest.main()
