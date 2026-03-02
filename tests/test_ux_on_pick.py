import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.bot import callbacks
from app.core import state


class TestUXOnPick(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.limiter = MagicMock()
        state.limiter.allow_user.return_value = True

    async def test_on_pick_shows_details(self):
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {"137": 1080, "140": None},
            "size_map": {"137": 52428800, "140": 5120000},
        }

        update = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        await callbacks.on_pick(update, context)

        calls = update.callback_query.edit_message_text.call_args_list
        self.assertTrue(len(calls) > 0)

        last_call = calls[-1]
        args, _ = last_call
        message_text = args[0]

        self.assertIn("1080p", message_text)
        self.assertIn("50.0 MB", message_text)
        self.assertIn("✅ <b>Готово", message_text)

    async def test_on_pick_has_link_and_tg_buttons_in_default_config(self):
        """Regression guard: default config keeps both action buttons."""
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "format_map": {"137": 1080},
            "title": "Test Video",
            "size_map": {"137": 52428800},
        }

        update = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        with patch("app.bot.callbacks.ENABLE_TELEGRAM_UPLOAD", True):
            await callbacks.on_pick(update, context)

        _, kwargs = update.callback_query.edit_message_text.call_args
        reply_markup = kwargs["reply_markup"]
        button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]

        self.assertIn("📥 Скачать (Ссылка)", button_texts)
        self.assertIn("📤 Отправить файл в TG", button_texts)

    async def test_on_pick_shows_audio_details(self):
        context = MagicMock()
        context.user_data = {
            "page_url": "http://example.com/video",
            "title": "Test Video",
            "format_map": {"140": None},
            "size_map": {"140": 5120000},
        }

        update = MagicMock()
        update.callback_query.data = "pick|140"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        with patch("app.bot.callbacks.AUDIO_FORMAT_ID", "140"):
            await callbacks.on_pick(update, context)

        calls = update.callback_query.edit_message_text.call_args_list
        last_call = calls[-1]
        message_text = last_call[0][0]

        self.assertIn("Audio", message_text)
        self.assertIn("4.9 MB", message_text)


if __name__ == "__main__":
    unittest.main()
