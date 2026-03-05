import unittest
import sys
import os
from unittest.mock import MagicMock

# Add repo root to path
# Mock dependencies before importing app code (setdefault to avoid corrupting real packages)
sys.modules.setdefault("dotenv", MagicMock())
sys.modules.setdefault("cachetools", MagicMock())
sys.modules.setdefault("yt_dlp", MagicMock())
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())
sys.modules.setdefault("fastapi", MagicMock())

# Set env vars
os.environ["BOT_TOKEN"] = "test_token"

# Now import
from app.core.utils import extract_supported_url

class TestExtractSupportedUrl(unittest.TestCase):
    def test_extract_valid_url(self):
        text = "Check out https://youtube.com/watch?v=123"
        expected = "https://youtube.com/watch?v=123"
        self.assertEqual(extract_supported_url(text), expected)

    def test_extract_pure_url(self):
        text = "https://vm.tiktok.com/123"
        expected = "https://vm.tiktok.com/123"
        self.assertEqual(extract_supported_url(text), expected)

    def test_no_url(self):
        text = "Hello world"
        self.assertIsNone(extract_supported_url(text))

    def test_unsupported_url(self):
        text = "Check this https://google.com"
        self.assertIsNone(extract_supported_url(text))

    def test_url_cleanup(self):
        text = "Link: https://youtube.com/watch?v=123."
        expected = "https://youtube.com/watch?v=123"
        self.assertEqual(extract_supported_url(text), expected)

if __name__ == "__main__":
    unittest.main()
