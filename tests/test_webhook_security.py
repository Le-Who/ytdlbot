from unittest.mock import AsyncMock

"""Tests for webhook endpoint — verifies real route behavior via FastAPI TestClient."""

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import configure_job_store, router
from app.core.config import TELEGRAM_SECRET_TOKEN

_app = FastAPI()
_app.include_router(router)


class TestWebhookEndpoint(unittest.TestCase):
    """Test the real /webhook endpoint via TestClient."""

    def setUp(self):
        configure_job_store(None)
        self.client = TestClient(_app)

    def tearDown(self):
        configure_job_store(None)

    def test_webhook_no_auth_header_returns_401(self):
        """Request WITHOUT auth header must be 401."""
        resp = self.client.post("/webhook", json={})
        self.assertEqual(resp.status_code, 401)

    def test_webhook_wrong_token_returns_401(self):
        """Request with WRONG auth header must be 401."""
        resp = self.client.post(
            "/webhook",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_webhook_empty_token_returns_401(self):
        """Empty string token must be 401."""
        resp = self.client.post(
            "/webhook",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": ""},
        )
        self.assertEqual(resp.status_code, 401)

    @patch("app.api.routes.state")
    def test_webhook_correct_token_returns_200(self, mock_state):
        """Request WITH correct auth header must be accepted."""

        class AcceptingStore:
            async def accept_update(self, payload):
                return object()

        configure_job_store(AcceptingStore())
        mock_state.bot_app = None
        mock_state.limiter.allow_ip = AsyncMock(return_value=True)
        resp = self.client.post(
            "/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET_TOKEN},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})

    @patch("app.api.routes.state")
    def test_webhook_without_durable_store_returns_retryable_503(self, mock_state):
        mock_state.bot_app = None
        mock_state.limiter.allow_ip = AsyncMock(return_value=True)

        resp = self.client.post(
            "/webhook",
            json={"update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET_TOKEN},
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers["retry-after"], "1")

    @patch("app.api.routes.state")
    def test_webhook_rate_limited_returns_429(self, mock_state):
        """Rate-limited request must be 429."""
        mock_state.limiter.allow_ip = AsyncMock(return_value=False)
        resp = self.client.post(
            "/webhook",
            json={"update_id": 1},
            headers={
                "X-Telegram-Bot-Api-Secret-Token": TELEGRAM_SECRET_TOKEN,
                "x-forwarded-for": "1.2.3.4",
            },
        )
        self.assertEqual(resp.status_code, 429)


if __name__ == "__main__":
    unittest.main()
