import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

from telegram import Message

os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.parsers import get_special_format
from app.bot.callbacks import on_send


class TestUXImprovement(unittest.TestCase):
    def test_audio_label_clean(self):
        """Verify audio label is clean '🎵 Audio' without '(best)'"""
        fmt = get_special_format("https://youtube.com/watch?v=123")
        self.assertEqual(fmt.label, "🎵 Audio")
        self.assertNotIn("(best)", fmt.label)

    def test_pinterest_label(self):
        """Verify Pinterest returns GIF format"""
        fmt = get_special_format("https://pinterest.com/pin/123")
        self.assertEqual(fmt.label, "🎬 Только GIF")


class TestOnSendUX(unittest.IsolatedAsyncioTestCase):
    async def test_on_send_toast(self):
        """Verify on_send shows a toast notification"""
        update = MagicMock()
        context = MagicMock()

        query = MagicMock()
        update.callback_query = query
        query.data = "send|token123"
        query.from_user.id = 12345
        query.message = MagicMock(spec=Message)
        query.message.chat_id = 99999

        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()

        with patch('app.bot.callbacks.state') as mock_state:
            # Mock limiter to reject (fail early)
            mock_state.limiter.allow_user.return_value = False
            mock_state.limiter.allow_chat.return_value = True

            await on_send(update, context)

            query.answer.assert_called_with("🚀 Загрузка началась")


if __name__ == '__main__':
    unittest.main()
