import unittest
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# Mock environment variables
os.environ["BOT_TOKEN"] = "test_token"
os.environ["WEBHOOK_URL"] = "https://example.com"
os.environ["TELEGRAM_SECRET_TOKEN"] = "secret"

# Mock external dependencies
telegram_mock = MagicMock()
sys.modules["telegram"] = telegram_mock
sys.modules["telegram.ext"] = MagicMock()


# Mock telegram.error.NetworkError as a real exception class
class MockNetworkError(Exception):
    pass


telegram_error_mock = MagicMock()
telegram_error_mock.NetworkError = MockNetworkError
sys.modules["telegram.error"] = telegram_error_mock

sys.modules["fastapi"] = MagicMock()
sys.modules["yt_dlp"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import app modules after mocking
from app.bot import callbacks, keyboards  # noqa: E402


class TestUXCloseButton(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock Context
        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        # Mock Update
        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.message = MagicMock()
        self.update.callback_query.message.delete = AsyncMock()

    async def test_on_close(self):
        # Setup
        self.update.callback_query.data = "close"

        # Execute
        await callbacks.on_close(self.update, self.context)

        # Verify
        self.update.callback_query.answer.assert_awaited_once()
        self.update.callback_query.message.delete.assert_awaited_once()

    def test_build_format_keyboard_has_close_button(self):
        # Setup
        formats = []
        special_format = MagicMock(label="Audio", format_id="audio")

        # We need to inspect the return value of build_format_keyboard
        # It returns InlineKeyboardMarkup(buttons)
        # buttons is a list of lists of InlineKeyboardButton

        # Since we mocked telegram, InlineKeyboardMarkup is a MagicMock
        # We need to see how it was called

        # We mock InlineKeyboardMarkup inside the function call to capture the arguments
        with patch("app.bot.keyboards.InlineKeyboardMarkup") as MockMarkup, patch(
            "app.bot.keyboards.InlineKeyboardButton"
        ) as MockButton:

            # Execute
            keyboards.build_format_keyboard(formats, special_format)

            # Verify InlineKeyboardMarkup was called
            args, _ = MockMarkup.call_args
            # buttons = args[0]

            # Check calls to InlineKeyboardButton
            # We expect one call with text="❌ Закрыть" and callback_data="close"

            found_close = False
            for call in MockButton.call_args_list:
                args, kwargs = call
                # Check args (positional) or kwargs
                # InlineKeyboardButton(text, callback_data=...)
                text = args[0] if args else kwargs.get("text")
                callback_data = kwargs.get("callback_data")

                if text == "❌ Закрыть" and callback_data == "close":
                    found_close = True
                    break

            self.assertTrue(
                found_close, "Close button not found in keyboard construction"
            )


if __name__ == "__main__":
    unittest.main()
