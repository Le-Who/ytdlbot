import unittest

# Add repo root to path
from app.core.utils import is_supported_url


class TestMainUtils(unittest.TestCase):
    def test_is_supported_url(self):
        # Supported exact
        self.assertTrue(is_supported_url("https://youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://tiktok.com/@user/video/123"))

        # Supported subdomain
        self.assertTrue(is_supported_url("https://www.youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://m.tiktok.com/v/123"))
        self.assertTrue(is_supported_url("https://vm.tiktok.com/123"))

        # Unsupported
        self.assertFalse(is_supported_url("https://google.com"))
        self.assertFalse(is_supported_url("https://example.com"))

        # Tricky cases (Suffix but not subdomain)
        self.assertFalse(is_supported_url("https://notyoutube.com"))
        self.assertFalse(is_supported_url("https://myyoutube.com"))
        self.assertFalse(is_supported_url("https://faketiktok.com"))

        # Malformed
        self.assertFalse(is_supported_url("not a url"))
        self.assertFalse(is_supported_url(""))

        # Case insensitivity (urlparse handles domain as lowercase usually, but good to check)
        self.assertTrue(is_supported_url("https://YOUTUBE.COM/watch"))


if __name__ == "__main__":
    unittest.main()
