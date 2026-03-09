"""Tests for API endpoints — /health, /metrics, /dl."""

import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes import router
from app.main import add_security_headers

_app = FastAPI()
_app.include_router(router)
_app.middleware("http")(add_security_headers)


class TestHealthEndpoint(unittest.TestCase):
    """Test /health returns correct JSON."""

    def setUp(self):
        self.client = TestClient(_app)

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_ok_true(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.json(), {"ok": True})

    def test_health_content_type_json(self):
        resp = self.client.get("/health")
        self.assertIn("application/json", resp.headers["content-type"])


class TestMetricsEndpoint(unittest.TestCase):
    """Test /metrics returns Prometheus-format text."""

    def setUp(self):
        self.client = TestClient(_app)

    @patch("app.api.routes.state")
    def test_metrics_returns_200(self, mock_state):
        """Metrics endpoint should return 200 with text/plain."""
        # Import metrics to ensure the module is available
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.headers["content-type"])

    @patch("app.api.routes.state")
    def test_metrics_body_not_empty(self, mock_state):
        """Metrics body should have content."""
        resp = self.client.get("/metrics")
        self.assertGreater(len(resp.text), 0)


class TestDownloadEndpointErrors(unittest.TestCase):
    """Test /dl/{token} error responses."""

    def setUp(self):
        self.client = TestClient(_app)

    def test_nonexistent_token_returns_404(self):
        """Token not in cache returns 404."""
        with patch("app.api.routes.state") as mock_state:
            mock_state.link_cache = {}
            resp = self.client.get("/dl/fake_token")
            self.assertEqual(resp.status_code, 404)
            self.assertIn("expired", resp.json()["detail"].lower())

    @patch("app.api.routes.state")
    def test_rate_limited_ip_returns_429(self, mock_state):
        """Rate-limited IP returns 429."""
        mock_state.link_cache = {"t1": {"page_url": "http://x.com", "title": "T"}}
        mock_state.limiter.allow_ip.return_value = False
        mock_state.limiter.allow_token.return_value = True
        resp = self.client.get("/dl/t1")
        self.assertEqual(resp.status_code, 429)

    @patch("app.api.routes.state")
    def test_rate_limited_token_returns_429(self, mock_state):
        """Rate-limited token returns 429."""
        mock_state.link_cache = {"t2": {"page_url": "http://x.com", "title": "T"}}
        mock_state.limiter.allow_ip.return_value = True
        mock_state.limiter.allow_token.return_value = False
        resp = self.client.get("/dl/t2")
        self.assertEqual(resp.status_code, 429)


if __name__ == "__main__":
    unittest.main()
