import asyncio
import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.bot import callbacks
from app.core import state


class TestUXOnSend(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.tasks_sem = asyncio.Semaphore(5)
        state.limiter = MagicMock()
        state.limiter.allow_user.return_value = True
        state.limiter.allow_chat.return_value = True

    async def test_on_send_success_has_back_button(self):
        context = MagicMock()
        context.user_data = {"size_map": {"137": 1024}}
        context.bot = AsyncMock()

        update = MagicMock()
        update.callback_query.data = "send|test_token"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.chat_id = 999
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.delete_message = AsyncMock()

        state.link_cache["test_token"] = {
            "page_url": "http://example.com/video",
            "format_id": "137",
            "title": "Test Video",
        }

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.mp4", None)
            mock_send.return_value = True

            await callbacks.on_send(update, context)

            mock_dl.assert_awaited_once()
            # After success, message gets deleted
            update.callback_query.delete_message.assert_awaited()

    async def test_on_send_error_shows_error_message(self):
        context = MagicMock()
        context.user_data = {"size_map": {"137": 1024}}
        context.bot = AsyncMock()

        update = MagicMock()
        update.callback_query.data = "send|test_token_err"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.chat_id = 999
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.delete_message = AsyncMock()

        state.link_cache["test_token_err"] = {
            "page_url": "http://example.com/video",
            "format_id": "137",
            "title": "Test Video",
        }

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = (None, "⚠️ Файл слишком большой.")

            await callbacks.on_send(update, context)

            calls = update.callback_query.edit_message_text.call_args_list
            last_call = calls[-1]
            args, _ = last_call
            self.assertIn("Файл слишком большой", args[0])


if __name__ == "__main__":
    unittest.main()
