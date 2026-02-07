import unittest
import sys
import os
from unittest.mock import MagicMock, AsyncMock

# Set env vars BEFORE imports
os.environ["BOT_TOKEN"] = "123:test"
os.environ["WEBHOOK_URL"] = "https://example.com"
os.environ["TELEGRAM_SECRET_TOKEN"] = "secret"

# MOCK EVERYTHING MISSING (for environments without dependencies installed)
# This allows testing pure logic and interaction flows without requiring external libs
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["telegram.error"] = MagicMock()
sys.modules["yt_dlp"] = MagicMock()
sys.modules["fastapi"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# We need to manually mock specific classes if used in base classes or decorators
sys.modules["cachetools"].TTLCache = MagicMock

from app.services.ytdlp.parsers import get_audio_format

# Now we can safely import app.bot.callbacks because dependencies are mocked
from app.bot.callbacks import on_send

class TestUXImprovement(unittest.TestCase):
    def test_audio_label_clean(self):
        """Verify audio label is clean '🎵 Только аудио' without '(best)'"""
        # Test generic URL (should return audio)
        fmt = get_audio_format("https://youtube.com/watch?v=123")
        self.assertEqual(fmt.label, "🎵 Только аудио")
        self.assertNotIn("(best)", fmt.label)

    def test_pinterest_label(self):
        """Verify Pinterest returns GIF format"""
        # Test Pinterest URL
        fmt = get_audio_format("https://pinterest.com/pin/123")
        self.assertEqual(fmt.label, "🎬 Только GIF")

class TestOnSendUX(unittest.IsolatedAsyncioTestCase):
    async def test_on_send_toast(self):
        """Verify on_send shows a toast notification"""
        # Mock Update and Context
        update = MagicMock()
        context = MagicMock()

        # Mock CallbackQuery
        query = MagicMock()
        update.callback_query = query
        query.data = "send|token123"
        query.from_user.id = 12345

        # Mock answer method
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        # Mock dependencies in callbacks
        with unittest.mock.patch('app.bot.callbacks.state') as mock_state, \
             unittest.mock.patch('app.bot.callbacks.check_rate_limit', return_value=False): # Fail early

            # Run handler
            await on_send(update, context)

            # Verify answer was called with the toast message
            query.answer.assert_called_with("🚀 Загрузка началась")

if __name__ == '__main__':
    unittest.main()
