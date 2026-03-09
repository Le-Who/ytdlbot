"""Tests for callback handler exception/malformed-data resilience.

Verifies that on_pick, on_cancel, on_send handle malformed/None callback data
gracefully: no crash, correct early return, and appropriate user feedback.
"""

import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from telegram import Message

from app.bot import callbacks
from app.core import state


class TestCallbacksExceptions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.ytdlp = MagicMock()
        state.tasks_sem = asyncio.Semaphore(5)

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

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
        self.update.callback_query.message = MagicMock(spec=Message)
        self.update.callback_query.message.chat_id = 99999
        self.update.callback_query.from_user = MagicMock()
        self.update.callback_query.from_user.id = 12345

    # --- on_pick ---
    async def test_on_pick_none_data_returns_silently(self):
        """on_pick with data=None answers the query, removes markup, then returns early (no edit_text)."""
        self.update.callback_query.data = None
        await callbacks.on_pick(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()
        # With None data, edit_message_text should NOT be called (no error message shown)
        self.update.callback_query.edit_message_text.assert_not_awaited()

    async def test_on_pick_malformed_data_no_crash(self):
        """on_pick with data lacking '|' separator logs error and returns without crash."""
        self.update.callback_query.data = "malformed_no_pipe"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_pick(self.update, self.context)
            # Should log the parsing error
            mock_logger.error.assert_called_once()
            self.assertIn("Invalid callback data", mock_logger.error.call_args[0][0])

    async def test_on_pick_valid_data_but_no_page_url_shows_expired(self):
        """on_pick with valid format but empty user_data shows data expired message."""
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {}  # No page_url
        await callbacks.on_pick(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("устарели", args[0].lower())

    # --- on_cancel ---
    async def test_on_cancel_none_data_returns_silently(self):
        """on_cancel with data=None answers with cancelling text, then returns early."""
        self.update.callback_query.data = None
        await callbacks.on_cancel(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()
        # No edit_message_text call for None data
        self.update.callback_query.edit_message_text.assert_not_awaited()

    async def test_on_cancel_malformed_data_logs_error(self):
        """on_cancel with malformed data logs error and returns without crash."""
        self.update.callback_query.data = "no_pipe_separator"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_cancel(self.update, self.context)
            mock_logger.error.assert_called_once()
            self.assertIn("Invalid callback data", mock_logger.error.call_args[0][0])

    async def test_on_cancel_valid_data_sets_cancel_flag(self):
        """on_cancel with valid data sets the cancel flag and shows cancelled message."""
        self.update.callback_query.data = "cancel|abc123"
        await callbacks.on_cancel(self.update, self.context)
        self.assertTrue(state.cancel_cache.get("abc123"))
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertEqual(args[0], "❌ Загрузка отменена пользователем.")

    # --- on_send ---
    async def test_on_send_none_data_returns_silently(self):
        """on_send with data=None answers 'download started' toast, then returns early."""
        self.update.callback_query.data = None
        await callbacks.on_send(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()

    async def test_on_send_malformed_data_no_crash(self):
        """on_send with data lacking '|' separator logs error and returns without crash."""
        self.update.callback_query.data = "send_no_pipe"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_send(self.update, self.context)
            mock_logger.error.assert_called_once()
            self.assertIn("Invalid callback data", mock_logger.error.call_args[0][0])

    async def test_on_send_missing_token_in_cache_shows_expired(self):
        """on_send with valid data format but nonexistent token shows link expired."""
        self.update.callback_query.data = "send|nonexistent_token"
        state.link_cache = {}
        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertEqual(args[0], "⚠️ Ссылка устарела.")


if __name__ == "__main__":
    unittest.main()
