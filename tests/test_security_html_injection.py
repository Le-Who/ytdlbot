import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock environment variables BEFORE importing app.main
with patch.dict(os.environ, {"BOT_TOKEN": "test_token", "WEBHOOK_URL": "https://example.com/webhook"}):
    from app.main import on_message

class TestHtmlInjection(unittest.IsolatedAsyncioTestCase):
    async def test_html_injection_in_title(self):
        # Mock update and context
        update = MagicMock()
        context = MagicMock()

        # User and Message
        update.effective_user.id = 12345
        update.message.text = "https://youtube.com/watch?v=malicious"

        # Mock reply_text to return a message object that we can track edit_text on
        msg_mock = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=msg_mock)

        # Mock ytdlp service to return malicious title
        # title, formats, audio, duration
        malicious_title = "<b>Bold</b> & <script>alert(1)</script>"
        formats = [MagicMock(format_id="1", label="720p", height=720)]
        audio = MagicMock(format_id="audio", label="Audio")
        duration = "1:00"

        # We need to mock asyncio.to_thread because it executes the function
        # Since we can't easily patch asyncio.to_thread globally without side effects,
        # we will patch ytdlp.list_formats.
        # asyncio.to_thread(func, *args) calls func(*args).

        with patch("app.main.ytdlp") as mock_ytdlp, \
             patch("app.main.info_cache", {}) as mock_cache, \
             patch("app.main.check_rate_limit", return_value=True):

            mock_ytdlp.list_formats = MagicMock(return_value=(malicious_title, formats, audio, duration))

            # Call the handler
            await on_message(update, context)

            # Check what msg.edit_text was called with
            if not msg_mock.edit_text.called:
                self.fail("msg.edit_text was not called")

            args, kwargs = msg_mock.edit_text.call_args
            actual_text = args[0]

            print(f"Actual text: {actual_text}")

            # Verification:
            # We assert that the title is escaped.
            # If it contains "<b>Bold</b>", it failed escaping.
            # It should contain "&lt;b&gt;Bold&lt;/b&gt;"

            if "<b>Bold</b>" in actual_text:
                self.fail("HTML Injection detected! Title was not escaped.")

            self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", actual_text)
            self.assertIn("&amp;", actual_text)
            self.assertIn("&lt;script&gt;", actual_text)

if __name__ == "__main__":
    unittest.main()
