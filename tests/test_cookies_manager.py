"""Tests for PlatformCookiesManager."""

import os
import unittest
from unittest.mock import patch

from app.services.ytdlp.cookies import PlatformCookiesManager

# Valid base64 of "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\ttest_cookie\tvalue"
_DUMMY_B64 = "IyBOZXRzY2FwZSBIVFRQIENvb2tpZSBGaWxlCi5leGFtcGxlLmNvbQlUUlVFCS8JRkFMU0UJMAlj b29raWVfbmFtZQljb29raWVfdmFsdWU="

class TestPlatformCookiesManager(unittest.TestCase):
    """Test cookie routing logic."""

    @patch.dict(os.environ, {}, clear=True)
    def test_no_cookies_configured(self):
        """No env vars → all paths return None."""
        mgr = PlatformCookiesManager()
        self.assertIsNone(mgr.get_cookies_path("https://www.youtube.com/watch?v=123"))
        self.assertIsNone(mgr.get_cookies_path("https://www.tiktok.com/@user/video/123"))
        self.assertIsNone(mgr.get_cookies_path("https://www.facebook.com/watch/123"))
        self.assertIsNone(mgr.tiktok_cookies_path)

    @patch.dict(os.environ, {"YTDLP_COOKIES_B64": _DUMMY_B64}, clear=True)
    def test_global_cookies_fallback(self):
        """Global cookies used for all platforms when no override."""
        mgr = PlatformCookiesManager()
        # YouTube should get global cookies
        path = mgr.get_cookies_path("https://www.youtube.com/watch?v=123")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))

        # TikTok also gets global cookies (no override)
        self.assertEqual(
            mgr.get_cookies_path("https://www.tiktok.com/@user/video/123"),
            path,
        )
        # tiktok_cookies_path shortcut also returns global
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

        # Both should be non-None
        self.assertIsNotNone(yt_path)
        self.assertIsNotNone(tt_path)

        # TikTok should get its own cookies, not the global ones
        self.assertNotEqual(yt_path, tt_path)

    @patch.dict(
        os.environ,
        {"FACEBOOK_COOKIES_B64": _DUMMY_B64},
        clear=True,
    )
    def test_facebook_cookies(self):
        """Facebook cookies routed by domain."""
        mgr = PlatformCookiesManager()

        # facebook.com
        fb_path = mgr.get_cookies_path("https://www.facebook.com/reel/123")
        self.assertIsNotNone(fb_path)

        # fb.watch (short URL)
        fbw_path = mgr.get_cookies_path("https://fb.watch/abc123/")
        self.assertIsNotNone(fbw_path)

        # Both should point to the same file (same env var)
        self.assertEqual(fb_path, fbw_path)

        # Unknown platform → None (no global configured)
        self.assertIsNone(
            mgr.get_cookies_path("https://www.youtube.com/watch?v=123")
        )

if __name__ == "__main__":
    unittest.main()
