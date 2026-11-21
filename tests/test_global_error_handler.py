import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["BOT_TOKEN"] = "test:token"
os.environ["WEBHOOK_URL"] = ""

from app.main import _global_error_handler


class TestGlobalErrorHandler(unittest.IsolatedAsyncioTestCase):
    async def test_global_error_handler_with_message(self):
        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            mock_logger.error.assert_called_once_with(
                "Unhandled exception in handler",
                exc_info=context.error,
                extra={"op": "error_handler"},
            )

            update.effective_message.reply_text.assert_called_once_with(
                "⚠️ Произошла внутренняя ошибка. Попробуйте позже."
            )

    async def test_global_error_handler_without_message(self):
        update = MagicMock()
        update.effective_message = None
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            mock_logger.error.assert_called_once()

    async def test_global_error_handler_reply_raises_exception(self):
        update = MagicMock()
        update.effective_message = AsyncMock()
        update.effective_message.reply_text.side_effect = Exception(
            "Failed to send message"
        )
        context = MagicMock()
        context.error = ValueError("Test error")

        with patch("app.main.logger") as mock_logger:
            await _global_error_handler(update, context)

            mock_logger.error.assert_called_once()

            update.effective_message.reply_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
