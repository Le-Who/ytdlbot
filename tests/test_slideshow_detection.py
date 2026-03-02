"""Tests for TikTok slideshow detection in parsers."""

import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.ytdlp.parsers import detect_tiktok_slideshow


class TestDetectTikTokSlideshow(unittest.TestCase):
    """Tests for detect_tiktok_slideshow()."""

    def test_non_tiktok_url_returns_false(self):
        info = {"formats": []}
        self.assertFalse(detect_tiktok_slideshow(info, "https://youtube.com/watch?v=123"))

    def test_tiktok_with_no_formats_returns_true(self):
        info = {"formats": []}
        self.assertTrue(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_tiktok_with_video_formats_returns_false(self):
        info = {
            "formats": [
                {"format_id": "1", "vcodec": "h264", "ext": "mp4"},
                {"format_id": "2", "vcodec": "h265", "ext": "mp4"},
            ]
        }
        self.assertFalse(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_tiktok_with_audio_only_returns_true(self):
        info = {
            "formats": [
                {"format_id": "1", "vcodec": "none", "ext": "m4a"},
                {"format_id": "2", "vcodec": "none", "ext": "mp3"},
            ]
        }
        self.assertTrue(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_tiktok_with_mixed_formats_returns_false(self):
        """If at least one format has video, it's not a slideshow."""
        info = {
            "formats": [
                {"format_id": "1", "vcodec": "none", "ext": "m4a"},
                {"format_id": "2", "vcodec": "h264", "ext": "mp4"},
            ]
        }
        self.assertFalse(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_tiktok_with_null_vcodec_returns_true(self):
        """Formats with vcodec=None (missing) are treated as non-video."""
        info = {
            "formats": [
                {"format_id": "1", "ext": "m4a"},
            ]
        }
        self.assertTrue(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_tiktok_missing_formats_key_returns_true(self):
        info = {}
        self.assertTrue(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_pinterest_url_returns_false(self):
        info = {"formats": []}
        self.assertFalse(
            detect_tiktok_slideshow(info, "https://pinterest.com/pin/123")
        )


if __name__ == "__main__":
    unittest.main()
