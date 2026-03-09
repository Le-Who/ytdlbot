"""Tests for security headers middleware — verifies REAL middleware from app.main."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes import router
from app.main import add_security_headers

_app = FastAPI()
_app.include_router(router)
_app.middleware("http")(add_security_headers)


class TestSecurityHeaders(unittest.TestCase):
    """Test the REAL add_security_headers middleware via TestClient."""

    def setUp(self):
        self.client = TestClient(_app)

    def test_health_gets_strict_csp(self):
        """Non-docs routes get strict CSP: default-src 'none'."""
        resp = self.client.get("/health")
        self.assertEqual(resp.headers["Content-Security-Policy"], "default-src 'none'")

    def test_health_gets_nosniff(self):
        """X-Content-Type-Options header is present on all routes."""
        resp = self.client.get("/health")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")

    def test_health_gets_deny_framing(self):
        """X-Frame-Options is DENY on all routes."""
        resp = self.client.get("/health")
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")

    def test_health_gets_referrer_policy(self):
        """Referrer-Policy is set correctly."""
        resp = self.client.get("/health")
        self.assertEqual(
            resp.headers["Referrer-Policy"],
            "strict-origin-when-cross-origin",
        )

    def test_docs_gets_relaxed_csp(self):
        """Docs route permits inline styles and cdn.jsdelivr.net."""
        resp = self.client.get("/docs")
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("'unsafe-inline'", csp)
        self.assertIn("cdn.jsdelivr.net", csp)
        self.assertIn("default-src 'self'", csp)

    def test_openapi_gets_relaxed_csp(self):
        """OpenAPI JSON route gets the relaxed CSP."""
        resp = self.client.get("/openapi.json")
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("'unsafe-inline'", csp)

    def test_arbitrary_path_gets_strict_csp(self):
        """Unknown path still gets strict CSP (even if 404)."""
        resp = self.client.get("/some/random/path")
        self.assertEqual(resp.headers["Content-Security-Policy"], "default-src 'none'")


if __name__ == "__main__":
    unittest.main()
