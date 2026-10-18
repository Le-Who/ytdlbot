"""Tests for TikTok content-type router and error classification."""

import unittest

from app.services.ytdlp.parsers import (
    classify_tiktok_content,
)


class TestClassifyTikTokContent(unittest.TestCase):
    def test_photo_url(self):
        self.assertEqual(
            classify_tiktok_content("https://www.tiktok.com/@user/photo/12345"),
            "slideshow",
        )

    def test_video_url(self):
        self.assertEqual(
            classify_tiktok_content("https://www.tiktok.com/@user/video/12345"),
            "video",
        )

    def test_short_url(self):
        """vm.tiktok.com short links → video."""
        self.assertEqual(
            classify_tiktok_content("https://vm.tiktok.com/ZM6abc123/"),
            "video",
        )

    def test_case_insensitive(self):
        self.assertEqual(
            classify_tiktok_content("https://www.tiktok.com/@user/PHOTO/12345"),
            "slideshow",
        )


if __name__ == "__main__":
    unittest.main()
