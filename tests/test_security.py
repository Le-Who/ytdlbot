import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import os
from fastapi.testclient import TestClient

# Ensure env vars are set before import if not already
if "BOT_TOKEN" not in os.environ:
    os.environ["BOT_TOKEN"] = "test-token"

from app.main import api


class TestSecurity(unittest.TestCase):

    @patch("app.main.WEBHOOK_URL", "https://example.com")
    @patch("app.main.TELEGRAM_SECRET_TOKEN", "test-secret")
    @patch("app.main.build_bot_app")
    def test_webhook_auth(self, mock_build):
        # Mock the bot app
        mock_bot_app = MagicMock()
        mock_bot_app.initialize = AsyncMock()
        mock_bot_app.start = AsyncMock()
        mock_bot_app.stop = AsyncMock()
        mock_bot_app.shutdown = AsyncMock()
        mock_bot_app.process_update = AsyncMock()
        mock_bot_app.bot = MagicMock()
        mock_bot_app.bot.set_webhook = AsyncMock()
        mock_bot_app.bot.delete_webhook = AsyncMock()
        mock_bot_app.updater = MagicMock()
        mock_bot_app.updater.running = False
        mock_bot_app.updater.start_polling = AsyncMock()
        mock_bot_app.updater.stop = AsyncMock()

        mock_build.return_value = mock_bot_app

        with TestClient(api) as client:
            # 1. No Header
            resp = client.post("/webhook", json={"update_id": 123})
            self.assertEqual(resp.status_code, 401)

            # 2. Invalid Header
            resp = client.post(
                "/webhook",
                json={"update_id": 123},
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            )
            self.assertEqual(resp.status_code, 401)

            # 3. Valid Header
            resp = client.post(
                "/webhook",
                json={"update_id": 123},
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True})

            # Verify set_webhook was called with secret token
            mock_bot_app.bot.set_webhook.assert_called_with(
                "https://example.com/webhook", secret_token="test-secret"
            )


if __name__ == "__main__":
    unittest.main()
