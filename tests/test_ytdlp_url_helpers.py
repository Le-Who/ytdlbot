import unittest
from app.services.ytdlp.parsers import (
    _is_tiktok,
    _is_youtube,
    _is_pinterest,
    _is_facebook,
)


class TestUrlHelpers(unittest.TestCase):
    def test_is_tiktok(self):
        # Valid URLs
        self.assertTrue(_is_tiktok("https://www.tiktok.com/@user/video/123"))
        self.assertTrue(_is_tiktok("http://tiktok.com/"))
        self.assertTrue(_is_tiktok("tiktok.com"))

        # Case insensitivity
        self.assertTrue(_is_tiktok("TiKtOk.CoM"))

        # Invalid URLs
        self.assertFalse(_is_tiktok("https://youtube.com"))
        self.assertFalse(_is_tiktok("example.com"))
        self.assertFalse(_is_tiktok(""))

    def test_is_youtube(self):
        # Valid URLs
        self.assertTrue(_is_youtube("https://www.youtube.com/watch?v=123"))
        self.assertTrue(_is_youtube("https://youtu.be/123"))
        self.assertTrue(_is_youtube("youtube.com"))
        self.assertTrue(_is_youtube("http://youtu.be/"))

        # Case insensitivity
        self.assertTrue(_is_youtube("YoUtUbE.cOm"))
        self.assertTrue(_is_youtube("YoUtU.bE"))

        # Invalid URLs
        self.assertFalse(_is_youtube("tiktok.com"))
        self.assertFalse(_is_youtube("google.com"))
        self.assertFalse(_is_youtube(""))

    def test_is_pinterest(self):
        # Valid URLs
        self.assertTrue(_is_pinterest("https://www.pinterest.com/pin/123"))
        self.assertTrue(_is_pinterest("https://pin.it/123"))

        # Case insensitivity
        self.assertTrue(_is_pinterest("PiNtErEsT.cOm"))
        self.assertTrue(_is_pinterest("PiN.iT"))

        # Invalid URLs
        self.assertFalse(
            _is_pinterest("pinterest.co.uk")
        )  # Based on current implementation which checks for .com specifically
        self.assertFalse(_is_pinterest("google.com"))
        self.assertFalse(_is_pinterest(""))

    def test_is_facebook(self):
        # Valid URLs
        self.assertTrue(_is_facebook("https://www.facebook.com/watch/123"))
        self.assertTrue(_is_facebook("https://m.facebook.com/reel/456"))
        self.assertTrue(_is_facebook("https://fb.watch/abc123/"))

        # Case insensitivity
        self.assertTrue(_is_facebook("FaCeBoOk.CoM"))
        self.assertTrue(_is_facebook("FB.WATCH"))

        # Invalid URLs
        self.assertFalse(_is_facebook("youtube.com"))
        self.assertFalse(_is_facebook("tiktok.com"))
        self.assertFalse(_is_facebook(""))


if __name__ == "__main__":
    unittest.main()
