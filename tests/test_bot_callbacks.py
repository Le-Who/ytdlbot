import asyncio
import unittest
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock environment variables
os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "secret")

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.bot import callbacks
from app.core import state
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from telegram import Message


class TestBotCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset state mocks for each test
        state.info_cache = {}
        state.link_cache = {}
        state.cancel_cache = {}
        state.ytdlp = MagicMock()
        state.tasks_sem = asyncio.Semaphore(5)

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

        # Mock limiter to allow by default
        state.limiter = MagicMock()
        state.limiter.allow_user.return_value = True
        state.limiter.allow_chat.return_value = True

        # Mock Context
        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        # Mock Update
        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
        self.update.callback_query.edit_message_reply_markup = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.message = MagicMock(spec=Message)
        self.update.callback_query.message.chat_id = 99999
        self.update.callback_query.from_user = MagicMock()
        self.update.callback_query.from_user.id = 12345

    async def test_on_back_cache_hit(self):
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.info_cache[page_url] = ("Test Title", mock_formats, mock_special_format, "10:00", False)

        with patch("app.bot.callbacks.build_format_keyboard") as mock_build_kb:
            mock_kb = MagicMock()
            mock_build_kb.return_value = mock_kb
            await callbacks.on_back(self.update, self.context)

            self.update.callback_query.answer.assert_awaited_once()
            self.update.callback_query.edit_message_text.assert_awaited_once()
            args, kwargs = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Test Title", args[0])
            self.assertEqual(kwargs["reply_markup"], mock_kb)

    async def test_on_back_cache_miss_success(self):
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.ytdlp.list_formats.return_value = ("Refreshed Title", mock_formats, mock_special_format, "5:00", False, None)

        with patch("app.bot.callbacks.build_format_keyboard"):
            await callbacks.on_back(self.update, self.context)
            state.ytdlp.list_formats.assert_called_with(page_url)
            self.assertIn(page_url, state.info_cache)
            self.assertEqual(state.info_cache[page_url][0], "Refreshed Title")

    async def test_on_back_cache_miss_failure(self):
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url
        state.ytdlp.list_formats.side_effect = Exception("API Error")

        await callbacks.on_back(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Ошибка обновления данных", args[0])

    async def test_on_back_missing_page_url(self):
        self.context.user_data = {}
        await callbacks.on_back(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Данные устарели", args[0])

    async def test_on_pick_success_video(self):
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {
            "page_url": "http://example.com/video",
            "format_map": {"137": 1080},
            "title": "Test Video",
            "size_map": {"137": 1024*1024*10}
        }

        await callbacks.on_pick(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()
        self.assertTrue(len(state.link_cache) > 0)
        token = list(state.link_cache.keys())[0]
        cached_data = state.link_cache[token]
        self.assertEqual(cached_data["format_id"], "137")

        args, kwargs = self.update.callback_query.edit_message_text.call_args
        self.assertIn("✅ <b>Готово", args[0])
        self.assertIn("1080p", args[0])

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

    async def test_on_send_rate_limit(self):
        self.update.callback_query.data = "send|token123"
        # Rate limit by making limiter reject
        state.limiter.allow_user.return_value = False
        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Слишком много запросов", args[0])

    async def test_on_send_queue_full(self):
        token = "token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {"page_url": "http://example.com", "format_id": "137"}
        state.tasks_sem = asyncio.Semaphore(0)  # Fully exhausted = queue full

        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Очередь переполнена", args[0])

    async def test_on_send_success_video(self):
        token = "valid_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "height": 1080,
            "title": "Video"
        }
        self.context.user_data = {"size_map": {"137": 100}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.mp4", None)
            mock_send.return_value = True

            await callbacks.on_send(self.update, self.context)
            mock_dl.assert_awaited_once()
            mock_send.assert_awaited_once()
            self.update.callback_query.delete_message.assert_awaited()

    async def test_on_send_success_audio(self):
        token = "audio_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": AUDIO_FORMAT_ID,
            "title": "Audio"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.mp3", None)
            mock_send.return_value = True

            await callbacks.on_send(self.update, self.context)
            mock_send.assert_awaited_once()
            # Verify is_audio=True was passed
            call_kwargs = mock_send.call_args[1]
            self.assertTrue(call_kwargs.get("is_audio"))

    async def test_on_send_success_gif(self):
        token = "gif_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": GIF_FORMAT_ID,
            "title": "GIF"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.gif", None)
            mock_send.return_value = True

            await callbacks.on_send(self.update, self.context)
            mock_send.assert_awaited_once()
            call_kwargs = mock_send.call_args[1]
            self.assertTrue(call_kwargs.get("is_gif"))

    async def test_on_send_download_failure(self):
        token = "fail_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = (None, "⚠️ Ошибка загрузки.")
            await callbacks.on_send(self.update, self.context)
            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Ошибка", args[0])

    async def test_on_send_file_too_large_pre_check(self):
        token = "large_file_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Big Video"
        }
        # > 50MB
        self.context.user_data = {"size_map": {"137": 60 * 1024 * 1024}}

        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Файл слишком большой", args[0])

    async def test_on_send_file_too_large_post_check(self):
        token = "large_post_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl:
            # File too large after download
            mock_dl.return_value = (None, "⚠️ Файл слишком большой.")
            await callbacks.on_send(self.update, self.context)
            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Файл слишком большой", args[0])

    async def test_on_send_progress_update_exception_handling(self):
        token = "progress_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "title": "Video"
        }
        self.context.user_data = {"size_map": {}}

        with patch("app.services.downloader.MediaSender.download_video", new_callable=AsyncMock) as mock_dl, \
             patch("app.services.downloader.MediaSender.send_file", new_callable=AsyncMock) as mock_send:
            mock_dl.return_value = ("/tmp/test.mp4", None)
            mock_send.return_value = True

            await callbacks.on_send(self.update, self.context)
            # The test validates that the handler completes without crashing
            mock_dl.assert_awaited_once()

    async def test_on_send_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_send(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_send", args[0])

    async def test_on_pick_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_pick(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_pick", args[0])

    async def test_on_cancel_malformed_data(self):
        self.update.callback_query.data = "invalid_data"
        with patch("app.bot.callbacks.logger") as mock_logger:
            await callbacks.on_cancel(self.update, self.context)
            mock_logger.error.assert_called()
            args, _ = mock_logger.error.call_args
            self.assertIn("Invalid callback data in on_cancel", args[0])

    async def test_on_pick_shows_send_to_tg_button_by_default(self):
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {
            "page_url": "http://example.com/video",
            "format_map": {"137": 1080},
            "title": "Test Video",
            "size_map": {"137": 1024 * 1024 * 10},
        }

        with patch("app.bot.callbacks.ENABLE_TELEGRAM_UPLOAD", True):
            await callbacks.on_pick(self.update, self.context)

        _, kwargs = self.update.callback_query.edit_message_text.call_args
        reply_markup = kwargs["reply_markup"]
        # Traverse the real InlineKeyboardMarkup structure
        button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
        self.assertIn("📤 Отправить файл в TG", button_texts)

    async def test_on_pick_hides_send_to_tg_button_when_flag_disabled(self):
        self.update.callback_query.data = "pick|137"
        self.context.user_data = {
            "page_url": "http://example.com/video",
            "format_map": {"137": 1080},
            "title": "Test Video",
            "size_map": {"137": 1024 * 1024 * 10},
        }

        with patch("app.bot.callbacks.ENABLE_TELEGRAM_UPLOAD", False):
            await callbacks.on_pick(self.update, self.context)

        _, kwargs = self.update.callback_query.edit_message_text.call_args
        reply_markup = kwargs["reply_markup"]
        button_texts = [btn.text for row in reply_markup.inline_keyboard for btn in row]
        self.assertNotIn("📤 Отправить файл в TG", button_texts)


if __name__ == "__main__":
    unittest.main()
