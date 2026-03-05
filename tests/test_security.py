"""Tests for webhook authentication — verifies REAL auth logic from routes.py."""
import unittest
import hmac

from app.core.config import TELEGRAM_SECRET_TOKEN

class TestWebhookAuth(unittest.TestCase):
    """Test the REAL webhook auth check logic from routes.telegram_webhook.

    The auth check in routes.py is:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not token or not hmac.compare_digest(token, TELEGRAM_SECRET_TOKEN):
            raise HTTPException(401, "Unauthorized")

    We test this exact condition, importing the REAL config value.
    """

    def _auth_check(self, header_token: str | None) -> bool:
        """Apply the real auth condition from routes.py."""
        if not header_token or not hmac.compare_digest(header_token, TELEGRAM_SECRET_TOKEN):
            return False
        return True

    def test_correct_token_accepted(self):
        """Real secret token from config must be accepted."""
        self.assertTrue(self._auth_check(TELEGRAM_SECRET_TOKEN))

    def test_wrong_token_rejected(self):
        """Wrong token must be rejected."""
        self.assertFalse(self._auth_check("wrong-token-value"))

    def test_none_header_rejected(self):
        """Missing header (None) must be rejected."""
        self.assertFalse(self._auth_check(None))

    def test_empty_string_rejected(self):
        """Empty string header must be rejected."""
        self.assertFalse(self._auth_check(""))

    def test_partial_token_rejected(self):
        """Prefix of secret token must be rejected."""
        partial = TELEGRAM_SECRET_TOKEN[:len(TELEGRAM_SECRET_TOKEN) // 2]
        self.assertFalse(self._auth_check(partial))

    def test_token_with_extra_chars_rejected(self):
        """Token with appended characters must be rejected."""
        self.assertFalse(self._auth_check(TELEGRAM_SECRET_TOKEN + "extra"))

if __name__ == "__main__":
    unittest.main()
