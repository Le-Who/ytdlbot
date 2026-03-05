"""Tests for TikTok content-type router and error classification."""

import unittest
from app.services.ytdlp.parsers import (
    _is_facebook,
    classify_tiktok_content,
    classify_tiktok_error,
    TikTokError,
)


class TestIsFacebook(unittest.TestCase):
    def test_facebook_com(self):
        self.assertTrue(_is_facebook("https://www.facebook.com/watch/123"))
        self.assertTrue(_is_facebook("https://m.facebook.com/reel/456"))
        self.assertTrue(_is_facebook("http://facebook.com/video/789"))

    def test_fb_watch(self):
        self.assertTrue(_is_facebook("https://fb.watch/abc123/"))

    def test_case_insensitive(self):
        self.assertTrue(_is_facebook("FaCeBoOk.CoM"))
        self.assertTrue(_is_facebook("FB.WATCH"))

    def test_non_facebook(self):
        self.assertFalse(_is_facebook("https://youtube.com"))
        self.assertFalse(_is_facebook("https://tiktok.com"))
        self.assertFalse(_is_facebook(""))


class TestClassifyTikTokContent(unittest.TestCase):
    def test_photo_url(self):
        self.assertEqual(
            classify_tiktok_content(
                "https://www.tiktok.com/@user/photo/12345"
            ),
            "slideshow",
        )

    def test_video_url(self):
        self.assertEqual(
            classify_tiktok_content(
                "https://www.tiktok.com/@user/video/12345"
            ),
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
            classify_tiktok_content(
                "https://www.tiktok.com/@user/PHOTO/12345"
            ),
            "slideshow",
        )


class TestClassifyTikTokError(unittest.TestCase):
    def test_auth_required(self):
        self.assertEqual(
            classify_tiktok_error("You need to log in to access"),
            TikTokError.AUTH_REQUIRED,
        )
        self.assertEqual(
            classify_tiktok_error("Please sign in first"),
            TikTokError.AUTH_REQUIRED,
        )
        self.assertEqual(
            classify_tiktok_error("cookies required"),
            TikTokError.AUTH_REQUIRED,
        )

    def test_auth_status_code(self):
        """TikTok 'Video not available, status code 10231' → AUTH."""
        self.assertEqual(
            classify_tiktok_error(
                "Video not available, status code 10231"
            ),
            TikTokError.AUTH_REQUIRED,
        )
        self.assertEqual(
            classify_tiktok_error("Content not available"),
            TikTokError.AUTH_REQUIRED,
        )

    def test_slideshow(self):
        self.assertEqual(
            classify_tiktok_error("Unsupported URL: /photo/"),
            TikTokError.SLIDESHOW,
        )

    def test_forbidden(self):
        self.assertEqual(
            classify_tiktok_error("HTTP Error 403: Forbidden"),
            TikTokError.FORBIDDEN,
        )

    def test_not_found(self):
        self.assertEqual(
            classify_tiktok_error("HTTP Error 404: Not Found"),
            TikTokError.NOT_FOUND,
        )

    def test_live(self):
        self.assertEqual(
            classify_tiktok_error("This is a live stream"),
            TikTokError.LIVE,
        )

    def test_live_not_triggered_by_available(self):
        """'live' + 'available' should route to AUTH_REQUIRED, not LIVE."""
        result = classify_tiktok_error("live version is not available")
        self.assertNotEqual(result, TikTokError.LIVE)
        self.assertEqual(result, TikTokError.AUTH_REQUIRED)

    def test_generic(self):
        self.assertEqual(
            classify_tiktok_error("Some unknown error occurred"),
            TikTokError.GENERIC,
        )


if __name__ == "__main__":
    unittest.main()
