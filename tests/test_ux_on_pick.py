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

class TestUXOnPick(unittest.IsolatedAsyncioTestCase):
    async def test_on_pick_shows_details(self):
        # Mock Context
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {
                "137": 1080,
                "140": None # Audio
            },
            "size_map": {
                "137": 52428800, # 50 MB
                "140": 5120000   # 5 MB
            }
        }

        # Mock Update
        update = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        await main.on_pick(update, context)

        # Check calls to edit_message_text
        calls = update.callback_query.edit_message_text.call_args_list
        self.assertTrue(len(calls) > 0)

        last_call = calls[-1]
        args, kwargs = last_call
        message_text = args[0]

        # Verify details are present
        self.assertIn("1080p", message_text, "Resolution 1080p should be in confirmation message")
        self.assertIn("50.0 MB", message_text, "Size 50.0 MB should be in confirmation message")
        self.assertIn("✅ <b>Готово", message_text, "Confirmation header should be present")

    async def test_on_pick_shows_audio_details(self):
        # Mock Context
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {
                "140": None # Audio
            },
            "size_map": {
                "140": 5120000   # ~4.9 MB
            }
        }

        # Mock Update
        update = MagicMock()
        update.callback_query.data = "pick|140"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        # Patch constants for AUDIO_FORMAT_ID if needed, but it's likely just checking ID
        with patch("app.main.AUDIO_FORMAT_ID", "140"):
            await main.on_pick(update, context)

        calls = update.callback_query.edit_message_text.call_args_list
        last_call = calls[-1]
        message_text = last_call[0][0]

        self.assertIn("Audio", message_text, "Audio label should be present")
        self.assertIn("4.9 MB", message_text, "Size should be present")

if __name__ == '__main__':
    unittest.main()
