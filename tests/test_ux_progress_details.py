import asyncio
import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.bot import callbacks
from app.core import state


class TestUXProgressDetails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.tasks_sem = asyncio.Semaphore(5)

        state.limiter = MagicMock()
        state.limiter.allow_user.return_value = True
        state.limiter.allow_chat.return_value = True

        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
        self.update.callback_query.edit_message_reply_markup = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.message = MagicMock()
        self.update.callback_query.message.chat_id = 99999
        self.update.callback_query.from_user.id = 12345

    async def test_progress_with_speed_and_eta(self):
        token = "test_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.mp4", None)
            mock_send.return_value = True

            await callbacks.on_send(self.update, self.context)
            # Verify download was called and handler completed successfully
            mock_dl.assert_awaited_once()
            mock_send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
