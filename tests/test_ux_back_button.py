import unittest
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock environment variables before importing app.main
with patch.dict(os.environ, {"BOT_TOKEN": "test_token"}):
    from app import main

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class TestUXBackButton(unittest.IsolatedAsyncioTestCase):
    async def test_on_back_restores_format_selection(self):
        # Mock Context
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
        }

        # Mock Update
        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        # Mock Cache
        # (title, formats, audio, duration)
        formats = [
            MagicMock(label="720p", format_id="137"),
            MagicMock(label="480p", format_id="136")
        ]
        audio = MagicMock(label="Audio", format_id="140")

        # We need to inject this into main.info_cache
        main.info_cache["http://example.com/video"] = ("Test Video", formats, audio, "05:00")

        # Call the handler
        if hasattr(main, 'on_back'):
            await main.on_back(update, context)

            # Verify answer was called
            update.callback_query.answer.assert_awaited_once()

            # Verify edit_message_text was called with correct text and buttons
            args, kwargs = update.callback_query.edit_message_text.call_args
            text = args[0]
            self.assertIn("Test Video", text)
            self.assertIn("05:00", text)

            reply_markup = kwargs.get('reply_markup')
            self.assertIsInstance(reply_markup, InlineKeyboardMarkup)
            # Check if buttons are correct
            buttons = reply_markup.inline_keyboard
            # formats[:6] + audio = 2 + 1 = 3 rows
            self.assertEqual(len(buttons), 3)
            self.assertEqual(buttons[0][0].text, "720p")
            self.assertEqual(buttons[2][0].text, "Audio")
        else:
            self.fail("main.on_back not implemented")

    async def test_on_pick_adds_back_button(self):
        # This tests existing on_pick modification
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {"137": 720}
        }

        update = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        # Mock link_cache
        main.link_cache = {}

        await main.on_pick(update, context)

        # Check if Back button is in the reply_markup
        args, kwargs = update.callback_query.edit_message_text.call_args
        reply_markup = kwargs.get('reply_markup')

        found_back = False
        if reply_markup:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data == "back":
                        found_back = True
                        break

        self.assertTrue(found_back, "Back button not found in on_pick response")
