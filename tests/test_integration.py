# mypy: ignore-errors
"""
Integration tests — verify complete user flows end-to-end.

These tests simulate the full message → list_formats → pick → send
pipeline through the real handler functions, verifying state transitions
and data flow between components.
"""

import unittest


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from telegram import Message

from app.bot import callbacks
from app.core import state
from app.core.utils import extract_supported_url, is_supported_url
from app.core.policy import size_allowed
from app.services.ytdlp.parsers import (
    get_special_format,
    deduplicate_formats,
)
from app.services.ytdlp.models import FormatItem, FormatMetadata, ExtractionResult
from app.bot.format_formatter import format_label
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID


class TestEndToEndListFormats(unittest.IsolatedAsyncioTestCase):
    """Integration: URL → list_formats → keyboard → pick flow."""

    async def asyncSetUp(self):
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)
        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)
        state.inflight_parsing = {}
        state.ytdlp = MagicMock()

    async def test_full_private_message_flow(self):
        """User picks format → link generated → feedback message sent (Strict AAA)."""
        # --- Arrange ---
        url = "https://youtube.com/watch?v=test123"
        formats = [
            FormatItem(
                format_id="137",
                ext="mp4",
                height=1080,
                filesize=100 * 1024 * 1024,
                protocol="https",
            ),
            FormatItem(
                format_id="136",
                ext="mp4",
                height=720,
                filesize=50 * 1024 * 1024,
                protocol="https",
            ),
        ]
        audio = FormatItem(
            format_id=AUDIO_FORMAT_ID,
            ext="audio",
            height=None,
            filesize=None,
            format_note="audio",
        )
        state.info_cache[url] = ExtractionResult(
            title="Test Video",
            formats=formats,
            special_format=audio,
            duration_str="05:30",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url="https://i.ytimg.com/vi/test123/maxresdefault.jpg",
        )

        update = MagicMock()
        context = MagicMock()
        update.callback_query.data = "pick|137"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message = MagicMock(spec=Message)
        update.callback_query.message.photo = ()
        update.callback_query.message.video = None
        update.callback_query.message.animation = None
        update.callback_query.message.document = None
        context.user_data = {
            "page_url": url,
            "title": "Test Video",
            "format_map": {"137": 1080, "136": 720},
            "size_map": {"137": 100 * 1024 * 1024, "136": 50 * 1024 * 1024},
        }

        # --- Act ---
        await callbacks.on_pick(update, context)

        # --- Assert ---
        self.assertTrue(len(state.link_cache) > 0)
        token = list(state.link_cache.keys())[0]
        cached = state.link_cache[token]
        self.assertEqual(cached.format_id, "137")
        self.assertEqual(cached.page_url, url)

        args, kwargs = update.callback_query.edit_message_text.call_args
        msg = args[0]
        self.assertIn("1080p", msg)
        self.assertIn("100.0 MB", msg)
        self.assertIn("Готово", msg)

    async def test_back_button_restores_format_selection(self):
        """After pick, pressing back shows format selection again."""
        url = "http://example.com/video"
        formats = [
            MagicMock(
                spec=FormatItem,
                format_id="137",
                height=1080,
                is_tiktok=False,
                filesize=None,
                protocol="https",
                format_note="",
            )
        ]
        audio = MagicMock(
            spec=FormatItem, format_id="audio", format_note="audio", is_tiktok=False
        )
        state.info_cache[url] = ExtractionResult(
            title="Video Title",
            formats=formats,
            special_format=audio,
            duration_str="03:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url=None,
        )

        update = MagicMock()
        context = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message = MagicMock(spec=Message)
        update.callback_query.message.photo = ()
        update.callback_query.message.video = None
        update.callback_query.message.animation = None
        update.callback_query.message.document = None
        context.user_data = {"page_url": url}

        with patch("app.bot.callbacks.build_format_keyboard") as mock_kb:
            mock_kb.return_value = MagicMock()
            await callbacks.on_back(update, context)

        args, kwargs = update.callback_query.edit_message_text.call_args
        self.assertIn("Video Title", args[0])
        self.assertIn("03:00", args[0])


class TestEndToEndDownload(unittest.IsolatedAsyncioTestCase):
    """Integration: pick → send → download → deliver flow."""

    async def asyncSetUp(self):
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.tasks_sem = asyncio.Semaphore(5)
        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)

    async def test_send_download_deliver_flow(self):
        """on_send downloads file and delivers it to user."""
        token = "integration_token"
        state.link_cache[token] = {
            "page_url": "http://example.com/video",
            "format_id": "137",
            "height": 1080,
            "title": "Integration Test Video",
        }

        update = MagicMock()
        context = MagicMock()
        update.callback_query.data = f"send|{token}"
        update.callback_query.from_user.id = 12345
        update.callback_query.message = MagicMock(spec=Message)
        update.callback_query.message.photo = ()
        update.callback_query.message.video = None
        update.callback_query.message.animation = None
        update.callback_query.message.document = None
        update.callback_query.message.chat_id = 99999
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.delete_message = AsyncMock()
        context.user_data = {"size_map": {}}
        context.bot = AsyncMock()

        with (
            patch(
                "app.services.downloader.MediaSender.download_video",
                new_callable=AsyncMock,
            ) as mock_dl,
            patch(
                "app.services.downloader.MediaSender.send_file", new_callable=AsyncMock
            ) as mock_send,
        ):
            mock_dl.return_value = ("/tmp/integration_test.mp4", None)
            mock_send.return_value = True

            await callbacks.on_send(update, context)

            mock_dl.assert_awaited_once()
            mock_send.assert_awaited_once()
            update.callback_query.delete_message.assert_awaited()

    async def test_cancel_mid_download(self):
        """Cancellation during download should stop the process."""
        token = "cancel_integration"

        update = MagicMock()
        context = MagicMock()
        update.callback_query.data = f"cancel|{token}"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()

        await callbacks.on_cancel(update, context)

        self.assertTrue(state.cancel_cache.get(token))
        args, _ = update.callback_query.edit_message_text.call_args
        self.assertIn("Загрузка отменена", args[0])

    async def test_rate_limit_rejects_request(self):
        """Rate-limited users see rejection message."""
        state.limiter.allow_user = AsyncMock(return_value=False)

        update = MagicMock()
        context = MagicMock()
        update.callback_query.data = "send|some_token"
        update.callback_query.from_user.id = 12345
        update.callback_query.message = MagicMock(spec=Message)
        update.callback_query.message.photo = ()
        update.callback_query.message.video = None
        update.callback_query.message.animation = None
        update.callback_query.message.document = None
        update.callback_query.message.chat_id = 99999
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()

        await callbacks.on_send(update, context)

        args, _ = update.callback_query.edit_message_text.call_args
        self.assertIn("Слишком много запросов", args[0])


class TestParserIntegration(unittest.TestCase):
    """Integration: URL detection → format parsing → deduplication → label generation."""

    def test_youtube_url_detection(self):
        self.assertTrue(is_supported_url("https://youtube.com/watch?v=abc"))
        self.assertTrue(is_supported_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_supported_url("https://youtu.be/abc"))

    def test_tiktok_url_detection(self):
        self.assertTrue(is_supported_url("https://www.tiktok.com/@user/video/123"))

    def test_unsupported_url_detection(self):
        self.assertFalse(is_supported_url("https://google.com"))
        self.assertFalse(is_supported_url("not a url"))

    def test_url_extraction_from_text(self):
        url = extract_supported_url(
            "Check this out https://youtube.com/watch?v=abc123 cool right?"
        )
        self.assertIsNotNone(url)
        self.assertIn("youtube.com", url)

    def test_format_label_generation(self):
        item = FormatItem(
            "id", "mp4", 1080, 100 * 1024 * 1024, is_tiktok=False, protocol="https"
        )
        label = format_label(item)
        self.assertIn("📺", label)
        self.assertIn("1080p", label)
        self.assertIn("100.0 MB", label)

    def test_tiktok_deduplication(self):
        """TikTok formats with same filesize are deduplicated."""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=720, filesize=5000, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=720, filesize=5000, protocol="https"
            ),
            FormatMetadata(
                format_id="3", ext="mp4", height=720, filesize=10000, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=True)
        self.assertEqual(len(result), 2)  # 5000 and 10000

    def test_non_tiktok_deduplication(self):
        """Non-TikTok formats are deduplicated by height."""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=1080, filesize=100, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=1080, filesize=200, protocol="https"
            ),
            FormatMetadata(
                format_id="3", ext="mp4", height=720, filesize=50, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 2)  # 1080 and 720

    def test_special_format_audio(self):
        fmt = get_special_format("https://youtube.com/watch?v=123")
        self.assertEqual(fmt.format_id, AUDIO_FORMAT_ID)
        self.assertEqual(fmt.format_note, "audio")

    def test_special_format_pinterest_gif(self):
        fmt = get_special_format("https://pinterest.com/pin/123")
        self.assertEqual(fmt.format_id, GIF_FORMAT_ID)
        self.assertEqual(fmt.format_note, "gif")


class TestPolicyIntegration(unittest.TestCase):
    """Integration: size policy checks."""

    def test_size_allowed_within_limit(self):
        self.assertTrue(size_allowed(10 * 1024 * 1024, target="telegram"))

    def test_size_allowed_exceeds_limit(self):
        self.assertFalse(size_allowed(200 * 1024 * 1024, target="telegram"))

    def test_size_allowed_none_filesize(self):
        """None filesize is allowed (optimistic approach with post-download check)."""
        self.assertTrue(size_allowed(None, target="telegram"))

    def test_size_allowed_exact_boundary(self):
        """Exactly at limit is allowed."""
        from app.core.config import MAX_TG_UPLOAD_MB

        exact = MAX_TG_UPLOAD_MB * 1024 * 1024
        self.assertTrue(size_allowed(exact, target="telegram"))
        self.assertFalse(size_allowed(exact + 1, target="telegram"))


if __name__ == "__main__":
    unittest.main()

