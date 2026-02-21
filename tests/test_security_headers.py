import os
import unittest

# Set environment variables BEFORE importing app.main to avoid runtime errors during import
os.environ["BOT_TOKEN"] = "test_token"
os.environ["WEBHOOK_URL"] = "https://example.com"

from fastapi.testclient import TestClient
from app.main import api


class TestSecurityHeaders(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api)

    def test_security_headers_present_health(self):
        """Test that strict CSP headers are present on /health."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

        headers = response.headers
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(
            headers.get("Referrer-Policy"), "strict-origin-when-cross-origin"
        )
        self.assertEqual(headers.get("Content-Security-Policy"), "default-src 'none'")

    def test_security_headers_relaxed_docs(self):
        """Test that relaxed CSP headers are present on /docs."""
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)

        headers = response.headers
        # Check that it allows 'unsafe-inline' and 'cdn.jsdelivr.net'
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("'unsafe-inline'", csp)
        self.assertIn("cdn.jsdelivr.net", csp)
        self.assertIn("default-src 'self'", csp)


if __name__ == "__main__":
    unittest.main()
