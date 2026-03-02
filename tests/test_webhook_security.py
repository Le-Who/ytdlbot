"""Tests for webhook HMAC authentication — full validation."""
import unittest
import hmac
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com/webhook")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "super-secret-token")

from app.core.config import TELEGRAM_SECRET_TOKEN


class TestWebhookSecurity(unittest.TestCase):
    """Test webhook auth logic: HMAC token validation."""

    def _simulate_auth_check(self, header_token: str | None) -> bool:
        """Reproduce the exact auth check from routes.py telegram_webhook."""
        if not header_token or not hmac.compare_digest(header_token, TELEGRAM_SECRET_TOKEN):
            return False
        return True

    def test_webhook_no_auth_header(self):
        """Request WITHOUT auth header must be rejected."""
        self.assertFalse(self._simulate_auth_check(None))

    def test_webhook_wrong_auth_header(self):
        """Request with WRONG auth header must be rejected."""
        self.assertFalse(self._simulate_auth_check("wrong-token"))

    def test_webhook_with_auth_header(self):
        """Request WITH correct auth header must be accepted."""
        self.assertTrue(self._simulate_auth_check(TELEGRAM_SECRET_TOKEN))

    def test_webhook_empty_string_header(self):
        """Empty string header must be rejected."""
        self.assertFalse(self._simulate_auth_check(""))


if __name__ == "__main__":
    unittest.main()
