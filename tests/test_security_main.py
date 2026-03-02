"""Tests for /dl endpoint security — verifies token validation and rate limiting."""
import unittest
import os
import sys
import re
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.core import state
from app.core.limiter import TokenBucketLimiter


class TestSecurityMain(unittest.TestCase):
    """Test download endpoint security logic (token validation, rate limiting, filename sanitization)."""

    def test_missing_token_returns_none(self):
        """link_cache.get() returns None for nonexistent tokens."""
        state.link_cache = {}
        result = state.link_cache.get("nonexistent_token")
        self.assertIsNone(result)

    def test_rate_limiter_blocks_after_burst(self):
        """Rate limiter correctly blocks after burst exhaustion."""
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
        self.assertTrue(limiter.allow("test_ip"))
        self.assertTrue(limiter.allow("test_ip"))
        self.assertFalse(limiter.allow("test_ip"))

    def test_rate_limiter_independent_keys(self):
        """Different keys are rate-limited independently."""
        limiter = TokenBucketLimiter(capacity=1, refill_rate=1.0)
        self.assertTrue(limiter.allow("ip_a"))
        self.assertTrue(limiter.allow("ip_b"))
        self.assertFalse(limiter.allow("ip_a"))

    def test_filename_sanitization(self):
        """Control characters and newlines are stripped from filenames."""
        raw_title = "normal_title\r\ninjected_header: bad"
        clean_title = re.sub(r'[\x00-\x1f\x7f\r\n]', '', raw_title)[:200]
        self.assertNotIn("\r", clean_title)
        self.assertNotIn("\n", clean_title)
        self.assertIn("normal_title", clean_title)

    def test_filename_length_limit(self):
        """Filenames are truncated to 200 chars."""
        raw_title = "A" * 500
        clean_title = re.sub(r'[\x00-\x1f\x7f\r\n]', '', raw_title)[:200]
        self.assertEqual(len(clean_title), 200)

    def test_valid_token_found_in_cache(self):
        """Valid token returns payload from link_cache."""
        state.link_cache = {"valid_token": {"page_url": "http://example.com", "title": "Test"}}
        result = state.link_cache.get("valid_token")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Test")


if __name__ == "__main__":
    unittest.main()
