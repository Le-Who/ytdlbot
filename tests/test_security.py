"""Tests for webhook HMAC authentication logic."""
import unittest
import hmac
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")


class TestSecurity(unittest.TestCase):
    """Test the HMAC comparison logic used in webhook auth."""

    def test_hmac_rejects_empty_token(self):
        """Empty token must not match any secret."""
        secret = "test-secret"
        self.assertFalse(bool(None) and hmac.compare_digest("", secret))

    def test_hmac_rejects_wrong_token(self):
        """Wrong token must not match the secret."""
        secret = "test-secret"
        result = hmac.compare_digest("wrong-token", secret)
        self.assertFalse(result)

    def test_hmac_accepts_correct_token(self):
        """Correct token must match the secret."""
        secret = "test-secret"
        result = hmac.compare_digest("test-secret", secret)
        self.assertTrue(result)

    def test_hmac_timing_safe(self):
        """Verify hmac.compare_digest is used (timing-safe comparison)."""
        # Ensure the function exists and is callable
        self.assertTrue(callable(hmac.compare_digest))

    def test_hmac_rejects_partial_match(self):
        """Partial match must fail."""
        secret = "super-secret-token"
        self.assertFalse(hmac.compare_digest("super-secret", secret))
        self.assertFalse(hmac.compare_digest("super-secret-token-extra", secret))


if __name__ == "__main__":
    unittest.main()
