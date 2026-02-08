import unittest
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Mock environment variables
os.environ["BOT_TOKEN"] = "test_token"
os.environ["WEBHOOK_URL"] = "https://example.com"
os.environ["TELEGRAM_SECRET_TOKEN"] = "secret"

# Mock external dependencies
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["telegram.error"] = MagicMock()
sys.modules["fastapi"] = MagicMock()
sys.modules["yt_dlp"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import app modules after mocking
from app.bot import callbacks
from app.core import state

class TestUXProgressDetails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.tasks_sem = MagicMock()
        state.tasks_sem.locked.return_value = False
        state.tasks_sem.__aenter__.return_value = None
        state.tasks_sem.__aexit__.return_value = None

        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.from_user.id = 12345

    async def test_progress_with_speed_and_eta(self):
        token = "test_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        # Line containing speed and ETA
        progress_line = b"[download]  50.0% of 10.00MiB at  2.50MiB/s ETA 00:10"

        mock_proc.stdout.readline = AsyncMock(side_effect=[progress_line, b""])
        mock_proc.wait = AsyncMock()

        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        with patch("app.bot.callbacks.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("builtins.open", MagicMock()), \
             patch("app.bot.callbacks.safe_remove", MagicMock()):

            await callbacks.on_send(self.update, self.context)

            # Check all calls to edit_message_text
            # We expect one of them to contain the speed and ETA
            calls = self.update.callback_query.edit_message_text.call_args_list

            found = False
            for call in calls:
                args, _ = call
                text = args[0]
                if "2.50MiB/s" in text and "00:10" in text:
                    found = True
                    break

            self.assertTrue(found, "Speed and ETA not found in progress update")

if __name__ == "__main__":
    unittest.main()
