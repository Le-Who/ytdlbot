import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.bot import callbacks
from app.core import state


class TestUXBackButton(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.limiter = MagicMock()
        state.limiter.allow_user.return_value = True

    async def test_on_back_restores_format_selection(self):
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
        }

        update = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        formats = [
            MagicMock(label="720p", format_id="137"),
            MagicMock(label="480p", format_id="136"),
        ]
        audio = MagicMock(label="Audio", format_id="140")

        state.info_cache["http://example.com/video"] = ("Test Video", formats, audio, "05:00", False, None)

        await callbacks.on_back(update, context)

        update.callback_query.answer.assert_awaited_once()

        args, kwargs = update.callback_query.edit_message_text.call_args
        text = args[0]
        self.assertIn("Test Video", text)
        self.assertIn("05:00", text)

        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        buttons = reply_markup.inline_keyboard
        # formats[:8] -> 2 items -> 1 row + audio row = 2 rows
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0][0].text, "720p")
        self.assertEqual(buttons[1][0].text, "Audio")

    async def test_on_pick_adds_back_button(self):
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {"137": 720},
            "size_map": {"137": 50 * 1024 * 1024},
        }

        update = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        state.link_cache = {}

        await callbacks.on_pick(update, context)

        args, kwargs = update.callback_query.edit_message_text.call_args
        reply_markup = kwargs.get("reply_markup")

        found_back = False
        if reply_markup:
            for row in reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data == "back":
                        found_back = True
                        break

        self.assertTrue(found_back, "Back button not found in on_pick response")


if __name__ == "__main__":
    unittest.main()
