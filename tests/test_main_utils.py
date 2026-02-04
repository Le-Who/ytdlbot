import unittest
import os
import sys
from unittest import mock

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set dummy env vars to avoid RuntimeError on import
os.environ["BOT_TOKEN"] = "test_token"

from app.main import is_supported_url

class TestMainUtils(unittest.TestCase):
    def test_is_supported_url(self):
        # Supported domains (exact matches)
        self.assertTrue(is_supported_url("https://youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://youtu.be/123"))
        self.assertTrue(is_supported_url("https://vk.com/video123"))
        self.assertTrue(is_supported_url("https://tiktok.com/@user/video/123"))
        self.assertTrue(is_supported_url("https://pinterest.com/pin/123"))
        self.assertTrue(is_supported_url("https://pin.it/123"))
        self.assertTrue(is_supported_url("https://rutube.ru/video/123"))

        # Supported subdomains
        self.assertTrue(is_supported_url("https://www.youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://m.youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://music.youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://vt.tiktok.com/123"))
        self.assertTrue(is_supported_url("https://vkvideo.ru/video123"))

        # Unsupported domains
        self.assertFalse(is_supported_url("https://google.com"))
        self.assertFalse(is_supported_url("https://example.com"))
        self.assertFalse(is_supported_url("https://notyoutube.com"))
        self.assertFalse(is_supported_url("https://youtube.com.evil.com"))
        self.assertFalse(is_supported_url("https://myyoutube.com")) # Should be false

        # Edge cases
        self.assertFalse(is_supported_url("not a url"))
        self.assertFalse(is_supported_url(""))
        self.assertFalse(is_supported_url(None))
        # The function expects a clean URL, usually extracted by the caller
        self.assertFalse(is_supported_url("Check this https://www.youtube.com"))

if __name__ == '__main__':
    unittest.main()
