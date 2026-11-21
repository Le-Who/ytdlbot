"""Tests for PlatformCookiesManager."""

import os
import unittest
from unittest.mock import patch

from app.services.ytdlp.cookies import PlatformCookiesManager

_DUMMY_B64 = "IyBOZXRzY2FwZSBIVFRQIENvb2tpZSBGaWxlCi5leGFtcGxlLmNvbQlUUlVFCS8JRkFMU0UJMAlj b29raWVfbmFtZQljb29raWVfdmFsdWU="


class TestPlatformCookiesManager(unittest.TestCase):
    """Test cookie routing logic."""

    @patch.dict(os.environ, {}, clear=True)
    def test_no_cookies_configured(self):
        """No env vars → all paths return None."""
        mgr = PlatformCookiesManager()
        self.assertIsNone(mgr.get_cookies_path("https://www.youtube.com/watch?v=123"))
        self.assertIsNone(
            mgr.get_cookies_path("https://www.tiktok.com/@user/video/123")
        )
        self.assertIsNone(mgr.get_cookies_path("https://www.facebook.com/watch/123"))
        self.assertIsNone(mgr.tiktok_cookies_path)

    @patch.dict(os.environ, {"YTDLP_COOKIES_B64": _DUMMY_B64}, clear=True)
    def test_global_cookies_fallback(self):
        """Global cookies used for all platforms when no override."""
        mgr = PlatformCookiesManager()
        path = mgr.get_cookies_path("https://www.youtube.com/watch?v=123")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))

        self.assertEqual(
            mgr.get_cookies_path("https://www.tiktok.com/@user/video/123"),
            path,
        )
        self.assertEqual(mgr.tiktok_cookies_path, path)

    @patch.dict(
        os.environ,
        {"YTDLP_COOKIES_B64": _DUMMY_B64, "TIKTOK_COOKIES_B64": _DUMMY_B64},
        clear=True,
    )
    def test_platform_override_takes_priority(self):
        """Platform-specific cookies override global for matching URL."""
        mgr = PlatformCookiesManager()
        yt_path = mgr.get_cookies_path("https://www.youtube.com/watch?v=123")
        tt_path = mgr.get_cookies_path("https://www.tiktok.com/@user/video/123")

        self.assertIsNotNone(yt_path)
        self.assertIsNotNone(tt_path)

        self.assertNotEqual(yt_path, tt_path)

    @patch.dict(
        os.environ,
        {"FACEBOOK_COOKIES_B64": _DUMMY_B64},
        clear=True,
    )
    def test_facebook_cookies(self):
        """Facebook cookies routed by domain."""
        mgr = PlatformCookiesManager()

        fb_path = mgr.get_cookies_path("https://www.facebook.com/reel/123")
        self.assertIsNotNone(fb_path)

        fbw_path = mgr.get_cookies_path("https://fb.watch/abc123/")
        self.assertIsNotNone(fbw_path)

        self.assertEqual(fb_path, fbw_path)

        self.assertIsNone(mgr.get_cookies_path("https://www.youtube.com/watch?v=123"))


if __name__ == "__main__":
    unittest.main()
