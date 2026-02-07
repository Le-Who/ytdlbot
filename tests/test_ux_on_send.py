import unittest
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from collections import deque

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock environment variables before importing app.main
with patch.dict(os.environ, {"BOT_TOKEN": "test_token"}):
    from app import main

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class TestUXOnSend(unittest.IsolatedAsyncioTestCase):
    @patch("app.main.run_subprocess")
    async def test_on_send_success_has_back_button(self, mock_run_subprocess):
        # Mock Context
        context = MagicMock()
        context.user_data = {"size_map": {"137": 1024}}
        context.bot.send_document = AsyncMock()

        # Mock Update
        update = MagicMock()
        update.callback_query.data = "send|test_token"
        update.callback_query.from_user.id = 12345
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.chat_id = 999

        # Mock Cache
        main.link_cache["test_token"] = {
            "page_url": "http://example.com/video",
            "format_id": "137",
            "title": "Test Video"
        }

        # Mock ytdlp
        main.ytdlp.build_command = MagicMock(return_value=["echo", "fake_download"])

        # Mock process
        proc_mock = MagicMock()
        proc_mock.returncode = 0
        proc_mock.stdout.readline = AsyncMock(side_effect=[b"[download] 50.0% of 10MiB", b""])
        proc_mock.wait = AsyncMock()

        # Mock run_subprocess generator
        async def gen(*args, **kwargs):
            yield proc_mock, deque()
        mock_run_subprocess.side_effect = gen

        # Mock os.path.getsize
        with patch("os.path.getsize", return_value=1024), \
             patch("builtins.open", MagicMock()), \
             patch("app.main.safe_remove", MagicMock()):

            await main.on_send(update, context)

            # Check calls to edit_message_text
            calls = update.callback_query.edit_message_text.call_args_list
            last_call = calls[-1]
            args, kwargs = last_call

            self.assertIn("✅ Видео отправлено!", args[0])
            reply_markup = kwargs.get('reply_markup')
            self.assertIsNotNone(reply_markup)

            # Check for Back button
            found_back = False
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data == "back":
                        found_back = True
            self.assertTrue(found_back)

    @patch("app.main.run_subprocess")
    async def test_on_send_error_has_download_link(self, mock_run_subprocess):
        # Mock Context
        context = MagicMock()
        context.user_data = {"size_map": {"137": 1024}}

        # Mock Update
        update = MagicMock()
        update.callback_query.data = "send|test_token_err"
        update.callback_query.from_user.id = 12345
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        # Mock Cache
        main.link_cache["test_token_err"] = {
            "page_url": "http://example.com/video",
            "format_id": "137",
            "title": "Test Video"
        }

        main.ytdlp.build_command = MagicMock(return_value=["echo", "fake_download"])

        # Mock process failure
        proc_mock = MagicMock()
        proc_mock.returncode = 1
        proc_mock.stdout.readline = AsyncMock(return_value=b"")
        proc_mock.wait = AsyncMock()

        # Mock run_subprocess to return stderr error
        async def gen(*args, **kwargs):
            yield proc_mock, deque([b"Error: File larger than max size"])
        mock_run_subprocess.side_effect = gen

        with patch("app.main.safe_remove", MagicMock()):
            await main.on_send(update, context)

            calls = update.callback_query.edit_message_text.call_args_list
            last_call = calls[-1]
            args, kwargs = last_call

            # Message should indicate error
            self.assertIn("Файл слишком большой", args[0])

            reply_markup = kwargs.get('reply_markup')
            self.assertIsNotNone(reply_markup)

            # Check for Download Link AND Back button
            found_dl = False
            found_back = False
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    if "Ссылка" in btn.text:
                        found_dl = True
                    if btn.callback_data == "back":
                        found_back = True

            self.assertTrue(found_dl, "Download link button missing on error")
            self.assertTrue(found_back, "Back button missing on error")
