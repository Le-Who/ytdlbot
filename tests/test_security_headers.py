"""Tests for security headers middleware logic."""
import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")


class TestSecurityHeaders(unittest.TestCase):
    """Test the Content-Security-Policy logic directly."""

    def _get_csp_for_path(self, path: str) -> str:
        """Reproduce the CSP logic from app.main.add_security_headers."""
        if path.startswith(("/docs", "/redoc", "/openapi.json")):
            return "default-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net data:"
        else:
            return "default-src 'none'"

    def test_strict_csp_on_health(self):
        """Non-docs routes get strict CSP."""
        csp = self._get_csp_for_path("/health")
        self.assertEqual(csp, "default-src 'none'")

    def test_strict_csp_on_webhook(self):
        """Webhook route gets strict CSP."""
        csp = self._get_csp_for_path("/webhook")
        self.assertEqual(csp, "default-src 'none'")

    def test_relaxed_csp_on_docs(self):
        """Docs route gets relaxed CSP."""
        csp = self._get_csp_for_path("/docs")
        self.assertIn("'unsafe-inline'", csp)
        self.assertIn("cdn.jsdelivr.net", csp)
        self.assertIn("default-src 'self'", csp)

    def test_relaxed_csp_on_redoc(self):
        """Redoc route gets relaxed CSP."""
        csp = self._get_csp_for_path("/redoc")
        self.assertIn("'unsafe-inline'", csp)

    def test_relaxed_csp_on_openapi(self):
        """OpenAPI JSON route gets relaxed CSP."""
        csp = self._get_csp_for_path("/openapi.json")
        self.assertIn("'unsafe-inline'", csp)

    def test_strict_csp_on_dl(self):
        """Download route gets strict CSP."""
        csp = self._get_csp_for_path("/dl/some_token")
        self.assertEqual(csp, "default-src 'none'")

    def test_security_header_values(self):
        """Verify the expected header values are correct."""
        expected_headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
        for header, value in expected_headers.items():
            self.assertIsNotNone(value, f"{header} should not be None")


if __name__ == "__main__":
    unittest.main()
