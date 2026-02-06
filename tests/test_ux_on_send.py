import unittest
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock environment variables before importing app.main
with patch.dict(os.environ, {"BOT_TOKEN": "test_token"}):
    from app import main

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class TestUXOnSend(unittest.IsolatedAsyncioTestCase):
    async def test_on_send_success_has_back_button(self):
        # Mock Context
        context = MagicMock()
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

        # Mock ytdlp and subprocess
        # We need to mock build_command and asyncio.create_subprocess_exec
        main.ytdlp.build_command = MagicMock(return_value=["echo", "fake_download"])

        # Mock asyncio.create_subprocess_exec to return a process that finishes successfully
        proc_mock = MagicMock()
        proc_mock.returncode = 0
        proc_mock.stdout.readline = AsyncMock(side_effect=[b"", b""]) # End of stream
        proc_mock.stderr.read = AsyncMock(return_value=b"")
        proc_mock.wait = AsyncMock()

        # Mock os.path.getsize to return small size
        with patch("asyncio.create_subprocess_exec", return_value=proc_mock), \
             patch("os.path.getsize", return_value=1024), \
             patch("builtins.open", MagicMock()), \
             patch("app.main.safe_remove", MagicMock()):

            await main.on_send(update, context)

            # Check calls to edit_message_text
            # The last call should be "✅ Видео отправлено!" with reply_markup
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

    async def test_on_send_error_has_download_link(self):
        # Mock Context
        context = MagicMock()

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

        # Mock process failure (return code 1)
        proc_mock = MagicMock()
        proc_mock.returncode = 1
        # Set stderr to simulate file size error
        # Note: app.main logic checks error_text.lower()
        proc_mock.stderr.read = AsyncMock(return_value=b"Error: File larger than max size")
        proc_mock.stdout.readline = AsyncMock(return_value=b"")
        proc_mock.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=proc_mock), \
             patch("app.main.safe_remove", MagicMock()):

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
