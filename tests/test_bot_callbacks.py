# mypy: ignore-errors
from unittest.mock import AsyncMock
import asyncio
import unittest


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


from unittest.mock import MagicMock, patch

# Mock environment variables
# Ensure app can be imported
from app.bot import callbacks
from app.core import state
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from telegram import Message
from app.services.ytdlp.models import ExtractionResult


class TestBotCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset state mocks for each test
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.ytdlp = MagicMock()
        state.ytdlp.list_formats = AsyncMock()
        state.tasks_sem = asyncio.Semaphore(5)
        state.download_sem = asyncio.Semaphore(5)
        state.api_sem = asyncio.Semaphore(10)
        # Reset queues each test so _queue_full tests don't pollute successors
        from app.core.download_queue import DownloadQueue
        state.download_queue = DownloadQueue(state.download_sem, max_queue_size=15)
        state.api_queue = DownloadQueue(state.api_sem, max_queue_size=15)

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

        # Mock limiter to allow by default
        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)

        # Mock Context
        self.context = MagicMock()
        self.context.user_data = {}
        self.context.bot = AsyncMock()

        # Mock Update
        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
        self.update.callback_query.edit_message_media = AsyncMock()
        self.update.callback_query.edit_message_reply_markup = AsyncMock()
        self.update.callback_query.delete_message = AsyncMock()
        self.update.callback_query.message = MagicMock(spec=Message)
        self.update.callback_query.message.photo = ()
        self.update.callback_query.message.video = None
        self.update.callback_query.message.animation = None
        self.update.callback_query.message.document = None
        self.update.callback_query.message.chat_id = 99999
        self.update.callback_query.from_user = MagicMock()
        self.update.callback_query.from_user.id = 12345

    async def test_on_back_cache_hit(self):
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.info_cache[page_url] = ExtractionResult(
            title="Test Title",
            formats=mock_formats,
            special_format=mock_special_format,
            duration_str="10:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url="https://example.com/thumb.jpg",
        )

        with patch("app.bot.callbacks.build_format_keyboard") as mock_build_kb:
            mock_kb = MagicMock()
            mock_build_kb.return_value = mock_kb
            await callbacks.on_back(self.update, self.context)

            self.update.callback_query.answer.assert_awaited_once()
            # With thumbnail, edit_message_media is called instead of edit_message_text
            self.update.callback_query.edit_message_media.assert_awaited_once()
            _, kwargs = self.update.callback_query.edit_message_media.call_args
            media = kwargs["media"]
            self.assertIn("Test Title", media.caption)
            self.assertEqual(kwargs["reply_markup"], mock_kb)

    async def test_on_back_cache_miss_success(self):
        page_url = "http://example.com/video"
        self.context.user_data["page_url"] = page_url

        mock_formats = [MagicMock(format_id="137", label="1080p")]
        mock_special_format = MagicMock(format_id="audio", label="Audio")
        state.ytdlp.list_formats.return_value = ExtractionResult(
            title="Refreshed Title",
            formats=mock_formats,
            special_format=mock_special_format,
            duration_str="5:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url="https://example.com/thumb.jpg",
        )

        with patch("app.bot.callbacks.build_format_keyboard"):
            await callbacks.on_back(self.update, self.context)
            state.ytdlp.list_formats.assert_called_with(page_url)
            cached = await state.info_cache.get(page_url)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.title, "Refreshed Title")
            self.assertIn(page_url, state.info_cache)

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
            "size_map": {"137": 1024 * 1024 * 10},
        }

        await callbacks.on_pick(self.update, self.context)
        self.update.callback_query.answer.assert_awaited()
        self.assertTrue(len(state.link_cache) > 0)
        token = list(state.link_cache.keys())[0]
        cached_data = state.link_cache[token]
        self.assertEqual(cached_data.format_id, "137")

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
        state.limiter.allow_user = AsyncMock(return_value=False)
        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Слишком много запросов", args[0])

    async def test_on_send_queue_full(self):
        token = "token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {"page_url": "http://example.com", "format_id": "137"}
        # Replace download_queue with one whose enqueue() rejects immediately
        from app.core.download_queue import DownloadQueue
        full_sem = asyncio.Semaphore(0)  # 0 capacity → wait path
        queue = DownloadQueue(full_sem, max_queue_size=0)  # 0 queue cap → immediate QUEUE_FULL
        state.download_queue = queue

        await callbacks.on_send(self.update, self.context)
        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Очередь", args[0])

    async def test_on_send_success_video(self):
        token = "valid_token"
        self.update.callback_query.data = f"send|{token}"
        state.link_cache[token] = {
            "page_url": "http://example.com",
            "format_id": "137",
            "height": 1080,
            "title": "Video",
        }
        self.context.user_data = {"size_map": {"137": 100}}

        with (
            patch(
                "app.services.downloader.MediaSender.download_video",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.services.downloader.MediaSender.send_file", new_callable=AsyncMock
            ) as mock_send,
        ):
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
            "title": "Audio",
        }
        self.context.user_data = {"size_map": {}}

        with (
            patch(
                "app.services.downloader.MediaSender.download_video",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.services.downloader.MediaSender.send_file", new_callable=AsyncMock
            ) as mock_send,
        ):
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
            "title": "GIF",
        }
        self.context.user_data = {"size_map": {}}

        with (
            patch(
                "app.services.downloader.MediaSender.download_video",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.services.downloader.MediaSender.send_file", new_callable=AsyncMock
            ) as mock_send,
        ):
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
            "title": "Video",
        }
        self.context.user_data = {"size_map": {}}

        with patch(
            "app.services.downloader.MediaSender.download_video", new_callable=AsyncMock
        ) as mock_dl:
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
            "title": "Big Video",
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
            "title": "Video",
        }
        self.context.user_data = {"size_map": {}}

        with patch(
            "app.services.downloader.MediaSender.download_video", new_callable=AsyncMock
        ) as mock_dl:
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
            "title": "Video",
        }
        self.context.user_data = {"size_map": {}}

        with (
            patch(
                "app.services.downloader.MediaSender.download_video",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.services.downloader.MediaSender.send_file", new_callable=AsyncMock
            ) as mock_send,
        ):
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
