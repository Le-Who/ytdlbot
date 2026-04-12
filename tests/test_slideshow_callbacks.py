# mypy: ignore-errors
from unittest.mock import AsyncMock

"""Tests for slideshow callbacks and downloader methods."""

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

from telegram import Message

from app.bot import callbacks
from app.core import state
from app.constants import SLIDESHOW_PHOTO_FORMAT_ID, SLIDESHOW_VIDEO_FORMAT_ID
from app.services.gallery_dl.service import SlideshowResult


class TestSlideshowCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.ytdlp = MagicMock()
        state.tasks_sem = asyncio.Semaphore(5)
        state.download_sem = asyncio.Semaphore(5)
        state.api_sem = asyncio.Semaphore(10)
        # Reset queues each test
        from app.core.download_queue import DownloadQueue
        state.download_queue = DownloadQueue(state.download_sem, max_queue_size=15)
        state.api_queue = DownloadQueue(state.api_sem, max_queue_size=15)
        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)

        self.context = MagicMock()
        self.context.user_data = {"page_url": "https://tiktok.com/@user/video/123"}
        self.context.bot = AsyncMock()

        self.update = MagicMock()
        self.update.callback_query = MagicMock()
        self.update.callback_query.answer = AsyncMock()
        self.update.callback_query.edit_message_text = AsyncMock()
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

    async def test_on_slideshow_photos_success(self):
        """Slideshow photo mode should download and send photos."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}"

        mock_result = SlideshowResult(
            images=["/tmp/1.jpg", "/tmp/2.jpg"],
            audio=None,
            title="Test",
        )

        with (
            patch(
                "app.bot.callbacks.MediaSender.download_slideshow",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.bot.callbacks.MediaSender.send_slideshow_photos",
                new_callable=AsyncMock,
            ) as mock_send,
            patch("app.bot.callbacks.MediaSender.cleanup_slideshow"),
        ):
            mock_dl.return_value = (mock_result, None)
            mock_send.return_value = True

            await callbacks.on_slideshow(self.update, self.context)

            mock_dl.assert_awaited_once()
            mock_send.assert_awaited_once()
            self.update.callback_query.delete_message.assert_awaited()

    async def test_on_slideshow_video_success(self):
        """Slideshow video mode should download, convert, and send video."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_VIDEO_FORMAT_ID}"

        mock_result = SlideshowResult(
            images=["/tmp/1.jpg", "/tmp/2.jpg"],
            audio="/tmp/audio.mp3",
            title="Test",
        )

        with (
            patch(
                "app.bot.callbacks.MediaSender.download_slideshow",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.bot.callbacks.MediaSender.images_to_video",
                new_callable=AsyncMock,
            ) as mock_convert,
            patch(
                "app.bot.callbacks.MediaSender.send_file",
                new_callable=AsyncMock,
            ) as mock_send,
            patch("app.bot.callbacks.MediaSender.cleanup_slideshow"),
            patch("app.bot.callbacks.safe_remove"),
        ):
            mock_dl.return_value = (mock_result, None)
            mock_convert.return_value = "/tmp/slideshow.mp4"
            mock_send.return_value = True

            await callbacks.on_slideshow(self.update, self.context)

            mock_dl.assert_awaited_once()
            mock_convert.assert_awaited_once_with(mock_result.images, mock_result.audio)
            mock_send.assert_awaited_once()
            self.update.callback_query.delete_message.assert_awaited()

    async def test_on_slideshow_download_error(self):
        """Download failure should show error to user."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}"

        with patch(
            "app.bot.callbacks.MediaSender.download_slideshow",
            new_callable=AsyncMock,
        ) as mock_dl:
            mock_dl.return_value = (None, "⚠️ Ошибка загрузки.")

            await callbacks.on_slideshow(self.update, self.context)

            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Ошибка", args[0])

    async def test_on_slideshow_rate_limited(self):
        """Rate-limited user should see error."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}"
        state.limiter.allow_user = AsyncMock(return_value=False)

        await callbacks.on_slideshow(self.update, self.context)

        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Слишком много", args[0])

    async def test_on_slideshow_missing_page_url(self):
        """Missing page_url should show expired data error."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}"
        self.context.user_data = {}

        await callbacks.on_slideshow(self.update, self.context)

        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Данные устарели", args[0])

    async def test_on_slideshow_queue_full(self):
        """Full queue should show queue full error."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}"
        # Replace download_queue with one that rejects immediately (queue cap = 0)
        from app.core.download_queue import DownloadQueue
        full_sem = asyncio.Semaphore(0)
        state.download_queue = DownloadQueue(full_sem, max_queue_size=0)

        await callbacks.on_slideshow(self.update, self.context)

        args, _ = self.update.callback_query.edit_message_text.call_args
        self.assertIn("Очередь", args[0])

    async def test_on_slideshow_video_conversion_failure(self):
        """Video conversion failure should show error."""
        self.update.callback_query.data = f"slideshow|{SLIDESHOW_VIDEO_FORMAT_ID}"

        mock_result = SlideshowResult(
            images=["/tmp/1.jpg"],
            audio=None,
            title="Test",
        )

        with (
            patch(
                "app.bot.callbacks.MediaSender.download_slideshow",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.bot.callbacks.MediaSender.images_to_video",
                new_callable=AsyncMock,
            ) as mock_convert,
            patch("app.bot.callbacks.MediaSender.cleanup_slideshow"),
        ):
            mock_dl.return_value = (mock_result, None)
            mock_convert.return_value = None  # Conversion failed

            await callbacks.on_slideshow(self.update, self.context)

            args, _ = self.update.callback_query.edit_message_text.call_args
            self.assertIn("Ошибка", args[0])


if __name__ == "__main__":
    unittest.main()
