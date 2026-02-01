import unittest
import os
import sys
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock

# Set environment variables BEFORE importing app.main
os.environ["BOT_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
os.environ["WEBHOOK_URL"] = "https://example.com/webhook"
os.environ["WEBHOOK_SECRET"] = "super_secret_token"

from app.main import api

class TestWebhookSecurity(unittest.TestCase):
    def setUp(self):
        # Start patching
        self.patcher = patch("telegram.ext.Application.builder")
        self.mock_builder = self.patcher.start()

        # Configure the mock
        mock_app = MagicMock()
        mock_app.bot.set_webhook = AsyncMock()
        mock_app.bot.delete_webhook = AsyncMock()
        mock_app.initialize = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        mock_app.updater.stop = AsyncMock()

        self.mock_builder.return_value.token.return_value.build.return_value = mock_app

        # We need to use the context manager to trigger startup events
        self.client = TestClient(api)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patcher.stop()

    def test_webhook_no_header(self):
        response = self.client.post("/webhook", json={"update_id": 1})
        # Expect 401 Unauthorized (currently will be 200)
        self.assertEqual(response.status_code, 401, "Should reject request without secret header")

    def test_webhook_wrong_header(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong_token"}
        response = self.client.post("/webhook", json={"update_id": 1}, headers=headers)
        self.assertEqual(response.status_code, 401, "Should reject request with wrong secret header")

    def test_webhook_correct_header(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "super_secret_token"}
        # We expect 200 OK.
        # Note: app.main logic will try to process update.
        # Since we mocked the bot app, it shouldn't crash.
        response = self.client.post("/webhook", json={"update_id": 1}, headers=headers)
        self.assertEqual(response.status_code, 200, "Should accept request with correct secret header")

if __name__ == "__main__":
    unittest.main()
