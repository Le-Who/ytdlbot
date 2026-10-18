import unittest

from app.services.ytdlp.parsers import (
    _is_pinterest,
    _is_tiktok,
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


if __name__ == "__main__":
    unittest.main()
