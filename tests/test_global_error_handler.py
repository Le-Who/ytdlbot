import unittest
import os
import sys
from unittest.mock import AsyncMock, patch, MagicMock

# Mock config before importing app.main
os.environ["BOT_TOKEN"] = "test:token"
os.environ["WEBHOOK_URL"] = ""

from app.main import _global_error_handler

class TestGlobalErrorHandler(unittest.IsolatedAsyncioTestCase):
    async def test_global_error_handler_with_message(self):
        # Setup mocks
        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            # Verify logger was called
            mock_logger.error.assert_called_once_with(
                "Unhandled exception in handler",
                exc_info=context.error,
                extra={"op": "error_handler"},
            )

            # Verify reply was sent
            update.effective_message.reply_text.assert_called_once_with(
                "⚠️ Произошла внутренняя ошибка. Попробуйте позже."
            )

    async def test_global_error_handler_without_message(self):
        # Setup mocks without effective_message
        update = MagicMock()
        update.effective_message = None
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            # Verify logger was called
            mock_logger.error.assert_called_once()
            # It shouldn't crash trying to reply

    async def test_global_error_handler_reply_raises_exception(self):
        # Setup mocks where reply_text raises an exception
        update = MagicMock()
        update.effective_message = AsyncMock()
        update.effective_message.reply_text.side_effect = Exception("Failed to send message")
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            # Verify logger was called
            mock_logger.error.assert_called_once()

            # Verify reply was attempted
            update.effective_message.reply_text.assert_called_once()

            # Should not raise exception out of _global_error_handler

if __name__ == "__main__":
    unittest.main()
