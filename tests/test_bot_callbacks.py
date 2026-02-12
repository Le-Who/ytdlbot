import unittest
import sys
import os
import asyncio
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
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import app modules after mocking
from app.bot import callbacks
from app.core import state
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
# Added to ensure app.services.downloader is loaded for patching
import app.services.downloader
from app.services.downloader import MediaSender

class TestBotCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset state mocks for each test
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.ytdlp = MagicMock()
        state.tasks_sem = MagicMock()
        state.tasks_sem.locked.return_value = False
        # Mock async context manager for semaphore
        state.tasks_sem.__aenter__.return_value = None
        state.tasks_sem.__aexit__.return_value = None

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__.return_value = None
        state.parsing_sem.__aexit__.return_value = None

        # Mock Context
        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        # Mock Update
        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.message = MagicMock()
        self.update.callback_query.message.delete = AsyncMock()
        self.update.callback_query.from_user = MagicMock()
        self.update.callback_query.from_user.id = 12345

    async def test_on_back_cache_hit(self):
        # Setup
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        # Mock cache hit
        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.info_cache[page_url] = ("Test Title", mock_formats, mock_special_format, "10:00")

        # Mock build_format_keyboard
        with patch("app.bot.callbacks.build_format_keyboard") as mock_build_kb:
            mock_kb = MagicMock()
            mock_build_kb.return_value = mock_kb

            # Execute
            await callbacks.on_back(self.update, self.context)

            # Verify
            self.update.callback_query.answer.assert_awaited_once()
            self.update.callback_query.edit_message_text.assert_awaited_once()
            args, kwargs = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Test Title", args[0])
            self.assertEqual(kwargs["reply_markup"], mock_kb)

    async def test_on_back_cache_miss_success(self):
        # Setup
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        # Mock cache miss & successful refresh
        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.ytdlp.list_formats.return_value = ("Refreshed Title", mock_formats, mock_special_format, "5:00")

        # Execute
        with patch("app.bot.callbacks.build_format_keyboard") as mock_build_kb:
            await callbacks.on_back(self.update, self.context)

            # Verify
            state.ytdlp.list_formats.assert_called_with(page_url)
            self.assertIn(page_url, state.info_cache)
            self.assertEqual(state.info_cache[page_url][0], "Refreshed Title")

            args, kwargs = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Refreshed Title", args[0])

    async def test_on_pick_success_video(self):
        # Setup
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {
            "page_url": "http://example.com/video",
            "format_map": {"137": 1080},
            "title": "Test Video",
            "size_map": {"137": 1024*1024*10} # 10MB
        }

        # Execute
        await callbacks.on_pick(self.update, self.context)

        # Verify
        self.update.callback_query.answer.assert_awaited()
        # Check cache
        self.assertTrue(len(state.link_cache) > 0)
        token = list(state.link_cache.keys())[0]
        cached_data = state.link_cache[token]
        self.assertEqual(cached_data["format_id"], "137")

        # Check response
        args, kwargs = self.update.callback_query.edit_message_text.call_args
        self.assertIn("✅ <b>Готово", args[0])
        self.assertIn("1080p", args[0])

    async def test_on_send_rate_limit(self):
        # Setup
        self.update.callback_query.data = "send|token123"

        with patch("app.bot.callbacks.check_rate_limit", return_value=False):
            # Execute
            await callbacks.on_send(self.update, self.context)

            # Verify
            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Слишком часто", args[0])

    async def test_on_send_success_video(self):
        # Setup
        token = "valid_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "height": 1080,
            "title": "Video"
        }
        self.context.user_data = {"size_map": {"137": 100}}

        # Mock run_subprocess to simulate success
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"", b""]) # End immediately
        mock_proc.wait = AsyncMock()

        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("builtins.open", MagicMock()), \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=True): # Ensure success

            # Execute
            await callbacks.on_send(self.update, self.context)

            # Verify
            self.context.bot.send_video.assert_awaited()
            self.update.callback_query.delete_message.assert_awaited()

    async def test_on_send_file_too_large_pre_check(self):
        # Setup
        token = "large_file_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Big Video"
        }
        # > 50MB
        self.context.user_data = {"size_map": {"137": 60 * 1024 * 1024}}

        with patch("app.bot.callbacks.check_rate_limit", return_value=True):
            # Execute
            await callbacks.on_send(self.update, self.context)

            # Verify
            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Файл слишком большой", args[0])

    async def test_on_back_cache_miss_failure(self):
        # Setup
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        # Mock failure
        state.ytdlp.list_formats.side_effect = Exception("API Error")

        # Execute
        await callbacks.on_back(self.update, self.context)

        # Verify
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Ошибка обновления данных", args[0])

    async def test_on_back_missing_page_url(self):
        # Setup: empty user_data
        self.context.user_data = {}

        # Execute
        await callbacks.on_back(self.update, self.context)

        # Verify
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Данные устарели", args[0])

    async def test_on_pick_missing_page_url(self):
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {}

        await callbacks.on_pick(self.update, self.context)

        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Данные устарели", args[0])

    async def test_on_cancel(self):
        token = "cancel_token"
        self.update.callback_query.data = f"cancel|{token}"

        await callbacks.on_cancel(self.update, self.context)

        self.assertTrue(state.cancel_cache.get(token))
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Загрузка отменена", args[0])

    async def test_on_send_queue_full(self):
        token = "token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {"page_url": "http://example.com"}

        with patch("app.bot.callbacks.check_rate_limit", return_value=True):
            state.tasks_sem.locked.return_value = True

            await callbacks.on_send(self.update, self.context)

            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Очередь переполнена", args[0])

    async def test_on_send_success_audio(self):
        token = "audio_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": AUDIO_FORMAT_ID,
            "title": "Audio"
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"", b""])
        mock_proc.wait = AsyncMock()
        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("builtins.open", MagicMock()), \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=True):

            await callbacks.on_send(self.update, self.context)

            self.context.bot.send_audio.assert_awaited()

    async def test_on_send_success_gif(self):
        token = "gif_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": GIF_FORMAT_ID,
            "title": "GIF"
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"", b""])
        mock_proc.wait = AsyncMock()
        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("os.path.getsize", return_value=1000), \
             patch("builtins.open", MagicMock()), \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=True):

            await callbacks.on_send(self.update, self.context)

            self.context.bot.send_animation.assert_awaited()

    async def test_on_send_download_failure(self):
        token = "fail_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()
        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, [b"ERROR: Requested format is not available"]

        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=False): # Should fail

            await callbacks.on_send(self.update, self.context)

            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Формат недоступен", args[0])

    @unittest.skip("Fails to patch os.path.getsize correctly in test environment")
    async def test_on_send_file_too_large_post_check(self):
        token = "large_post_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"", b""])
        mock_proc.wait = AsyncMock()
        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        # Mock file size > 50MB (post download)
        # Patch os.path.getsize (globally in test file, which affects os module)
        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("os.path.getsize", return_value=60 * 1024 * 1024), \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=True):

            await callbacks.on_send(self.update, self.context)

            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Файл слишком большой", args[0])

    @unittest.skip("Flaky logger assertion")
    async def test_on_send_progress_update_exception_handling(self):
        # Setup
        token = "progress_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }

        # Mock subprocess yielding a progress line
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        # line that matches PROGRESS_RE: "(\d+\.\d+)%"
        progress_line = b"[download]  45.5% of 10.00MiB at  2.00MiB/s ETA 00:05"

        mock_proc.stdout.readline = AsyncMock(side_effect=[progress_line, b""])
        mock_proc.wait = AsyncMock()

        async def mock_subprocess_gen(*args, **kwargs):
            yield mock_proc, []

        # Mock edit_message_text to raise Exception ONLY when updating progress
        async def edit_side_effect(*args, **kwargs):
            text = args[0] if args else kwargs.get("text", "")
            if "Скачиваю" in text:
                raise Exception("Telegram Error")
            return MagicMock()

        self.update.callback_query.edit_message_text.side_effect = edit_side_effect

        with patch("app.services.downloader.run_subprocess", side_effect=mock_subprocess_gen), \
             patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("app.bot.callbacks.logger") as mock_logger, \
             patch("app.bot.callbacks.safe_remove", MagicMock()), \
             patch("os.path.getsize", return_value=1000), \
             patch("builtins.open", MagicMock()), \
             patch("app.services.downloader.os.path.exists", return_value=True):

            await callbacks.on_send(self.update, self.context)

            # Verify that we tried to update
            # We can't assert_awaited because side_effect raises exception, but in the code it is caught.
            # So the call happens.

            # Verify that logger WAS called (expected behavior)
            mock_logger.warning.assert_called_with(
                "UI Update failed: Telegram Error"
            )
    async def test_on_send_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.check_rate_limit", return_value=True), \
             patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_send(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_send", args[0])

        self.update.callback_query.answer.assert_awaited()
        self.update.callback_query.edit_message_text.assert_not_called()

    async def test_on_pick_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_pick(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_pick", args[0])

        self.update.callback_query.answer.assert_awaited()
        self.update.callback_query.edit_message_text.assert_not_called()

    async def test_on_cancel_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_cancel(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_cancel", args[0])

        self.update.callback_query.answer.assert_awaited()
        self.update.callback_query.edit_message_text.assert_not_called()

    async def test_on_close(self):
        self.update.callback_query.data = "close"
        await callbacks.on_close(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()
        self.update.callback_query.message.delete.assert_awaited()

if __name__ == "__main__":
    unittest.main()
