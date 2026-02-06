import os
import unittest
from unittest.mock import patch, MagicMock

# Set required env vars before importing app.main
os.environ["BOT_TOKEN"] = "test_token"
os.environ["WEBHOOK_URL"] = "https://example.com/webhook"
os.environ["TELEGRAM_SECRET_TOKEN"] = "super-secret-token"

from fastapi.testclient import TestClient
import app.main
from app.main import api

class TestWebhookSecurity(unittest.TestCase):
    def setUp(self):
        # Patch the SECRET TOKEN in the already loaded module
        self.token_patcher = patch.object(app.main, 'TELEGRAM_SECRET_TOKEN', 'super-secret-token')
        self.token_patcher.start()
        self.client = TestClient(api)

    def tearDown(self):
        self.token_patcher.stop()

    def test_webhook_no_auth_header(self):
        """Test that webhook REJECTS request WITHOUT auth header"""
        response = self.client.post("/webhook", json={"update_id": 123, "message": {"text": "test"}})
        self.assertEqual(response.status_code, 401, "Webhook should reject request without authentication")

    def test_webhook_wrong_auth_header(self):
        """Test that webhook REJECTS request with WRONG auth header"""
        response = self.client.post(
            "/webhook",
            json={"update_id": 123, "message": {"text": "test"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"}
        )
        self.assertEqual(response.status_code, 401, "Webhook should reject request with wrong authentication")

    def test_webhook_with_auth_header(self):
        """Test that webhook ACCEPTS request WITH correct auth header"""
        response = self.client.post(
            "/webhook",
            json={"update_id": 123, "message": {"text": "test"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "super-secret-token"}
        )
        self.assertEqual(response.status_code, 200)

if __name__ == '__main__':
    unittest.main()
