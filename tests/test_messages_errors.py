"""Extended tests for app.bot.messages — error paths and edge cases."""

import asyncio
import unittest


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


from unittest.mock import MagicMock, AsyncMock, patch

from app.core import state
from app.core.texts import Texts
from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
)


class TestOnMessageErrorPaths(unittest.IsolatedAsyncioTestCase):
    """Test on_message error paths that were previously uncovered."""

    async def asyncSetUp(self):
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.prefs_cache = AsyncMockCache()  # needed by user_prefs fast-path in on_message
        state.inflight_parsing = {}
        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)
        state.parsing_sem = asyncio.Semaphore(5)
        state.ytdlp = MagicMock()
        state.ytdlp_executor = None

        self.context = MagicMock()
        self.context.bot = AsyncMock()
        self.context.user_data = {}

        self.update = MagicMock()
        self.update.effective_user.id = 123
        self.update.effective_chat.id = 456
        self.update.message.text = "https://youtube.com/watch?v=abc"
        self.update.message.caption = None
        self.update.message.reply_to_message = None
        self.update.message.message_id = 1
        self.update.message.reply_text = AsyncMock()
        self.update.message.reply_photo = AsyncMock()

        self.status_msg = AsyncMock()
        self.update.message.reply_text.return_value = self.status_msg

    async def test_access_denied_error(self):
        """AccessDeniedError shows access denied text."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(
            side_effect=AccessDeniedError("Login required")
        )
        await on_message(self.update, self.context)

        self.status_msg.edit_text.assert_awaited_with(Texts.ACCESS_DENIED)

    async def test_video_not_found_error(self):
        """VideoNotFoundError shows not found text."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(side_effect=VideoNotFoundError("404"))
        await on_message(self.update, self.context)

        self.status_msg.edit_text.assert_awaited_with(Texts.VIDEO_NOT_FOUND)

    async def test_live_stream_error(self):
        """LiveStreamError shows live not supported text."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(side_effect=LiveStreamError("Live stream"))
        await on_message(self.update, self.context)

        self.status_msg.edit_text.assert_awaited_with(Texts.LIVE_NOT_SUPPORTED)

    async def test_extraction_error_pinterest(self):
        """ExtractionError with 'pinterest' shows pinterest error text."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(
            side_effect=ExtractionError("pinterest: failed to extract")
        )
        await on_message(self.update, self.context)

        self.status_msg.edit_text.assert_awaited_with(Texts.PINTEREST_ERROR)

    async def test_extraction_error_generic(self):
        """Generic ExtractionError shows the error message."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(
            side_effect=ExtractionError("some random error")
        )
        await on_message(self.update, self.context)

        args = self.status_msg.edit_text.call_args[0][0]
        self.assertIn("some random error", args)

    async def test_generic_exception(self):
        """Unknown exception shows generic error with detail."""
        from app.bot.messages import on_message

        state.ytdlp.list_formats = AsyncMock(
            side_effect=RuntimeError("unexpected crash")
        )
        await on_message(self.update, self.context)

        args = self.status_msg.edit_text.call_args[0][0]
        self.assertIn("unexpected crash", args)

    async def test_timeout_error(self):
        """asyncio.TimeoutError shows timeout text."""
        from app.bot.messages import on_message

        with patch("app.bot.messages.asyncio.timeout") as mock_timeout:
            mock_timeout.return_value.__aenter__ = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            await on_message(self.update, self.context)

        self.status_msg.edit_text.assert_awaited_with(Texts.TIMEOUT_UNAVAILABLE)

    async def test_cached_url_shows_format_keyboard(self):
        """Cached URL shows format keyboard immediately."""
        from app.bot.messages import on_message
        from app.services.ytdlp.models import FormatItem

        fmt = FormatItem(
            format_id="137",
            ext="mp4",
            height=1080,
            filesize=50_000_000,
        )
        special = FormatItem(
            format_id="audio",
            ext="audio",
            height=None,
            filesize=None,
            format_note="audio",
        )

        from app.services.ytdlp.models import ExtractionResult

        state.info_cache["https://youtube.com/watch?v=abc"] = ExtractionResult(
            title="Test Video",
            formats=[fmt],
            special_format=special,
            duration_str="05:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url=None,
            tiktok_auth_error=False,
        )

        await on_message(self.update, self.context)

        # Should show format keyboard via edit_text (no thumbnail)
        args = self.status_msg.edit_text.call_args[0][0]
        self.assertIn("Test Video", args)

    async def test_cached_url_slideshow_shows_slideshow_keyboard(self):
        """Cached slideshow URL shows slideshow choice keyboard."""
        from app.bot.messages import on_message
        from app.services.ytdlp.models import FormatItem

        special = FormatItem(
            format_id="audio",
            ext="audio",
            height=None,
            filesize=None,
            format_note="audio",
        )

        from app.services.ytdlp.models import ExtractionResult

        state.info_cache["https://youtube.com/watch?v=abc"] = ExtractionResult(
            title="Slideshow",
            formats=[],
            special_format=special,
            duration_str="00:30",
            is_slideshow=True,
            info_json_path=None,
            thumbnail_url=None,
            tiktok_auth_error=False,
        )

        await on_message(self.update, self.context)

        args = self.status_msg.edit_text.call_args[0][0]
        self.assertIn("Slideshow", args)

    async def test_cached_url_with_thumbnail_sends_photo(self):
        """Cached URL with thumbnail sends photo instead of text."""
        from app.bot.messages import on_message
        from app.services.ytdlp.models import FormatItem

        fmt = FormatItem(
            format_id="137",
            ext="mp4",
            height=1080,
            filesize=50_000_000,
        )
        special = FormatItem(
            format_id="audio",
            ext="audio",
            height=None,
            filesize=None,
            format_note="audio",
        )

        from app.services.ytdlp.models import ExtractionResult

        state.info_cache["https://youtube.com/watch?v=abc"] = ExtractionResult(
            title="Test Video",
            formats=[fmt],
            special_format=special,
            duration_str="05:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url="https://example.com/thumb.jpg",
            tiktok_auth_error=False,
        )

        await on_message(self.update, self.context)

        # Should use reply_photo for thumbnail
        self.update.message.reply_photo.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
