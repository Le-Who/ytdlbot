import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock environment variables
os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.bot import callbacks
from app.core import state


class TestCallbacksExceptions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.ytdlp = MagicMock()
        state.tasks_sem = MagicMock()
        state.tasks_sem.locked.return_value = False
        state.tasks_sem.__aenter__ = AsyncMock(return_value=None)
        state.tasks_sem.__aexit__ = AsyncMock(return_value=None)

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

        # Mock limiter to allow by default
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
        self.update.callback_query.from_user = MagicMock()
        self.update.callback_query.from_user.id = 12345

    async def test_on_pick_malformed_data(self):
        self.update.callback_query.data = "malformed_data"
        await callbacks.on_pick(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_pick_none_data(self):
        self.update.callback_query.data = None
        await callbacks.on_pick(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_cancel_malformed_data(self):
        self.update.callback_query.data = "malformed_data"
        await callbacks.on_cancel(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_cancel_none_data(self):
        self.update.callback_query.data = None
        await callbacks.on_cancel(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_send_malformed_data(self):
        self.update.callback_query.data = "malformed_data"
        await callbacks.on_send(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_send_none_data(self):
        self.update.callback_query.data = None
        await callbacks.on_send(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
