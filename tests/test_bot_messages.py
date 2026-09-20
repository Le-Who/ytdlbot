from unittest.mock import AsyncMock

"""Tests for on_message handler — the core user flow."""

import unittest
from types import SimpleNamespace


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value, **kwargs):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


from unittest.mock import MagicMock, patch

from app.bot import messages
from app.core import state
from app.core.texts import Texts


class TestOnMessage(unittest.IsolatedAsyncioTestCase):
    """Test the on_message handler for the happy path, edge cases, and errors."""

    async def asyncSetUp(self):
        state.info_cache = AsyncMockCache()
        state.link_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.prefs_cache = (
            AsyncMockCache()
        )  # needed by user_prefs fast-path in on_message
        state.inflight_parsing = {}
        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)

        state.parsing_sem = MagicMock()
        state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
        state.parsing_sem.__aexit__ = AsyncMock(return_value=None)

        self.context = MagicMock()
        self.context.bot = AsyncMock()
        self.context.user_data = {}

        self.update = MagicMock()
        self.update.effective_user.id = 12345
        self.update.effective_chat.id = 99999
        self.update.effective_chat.type = "private"
        self.update.message.message_id = 1
        self.update.message.caption = None
        self.update.message.reply_to_message = None
        self.update.message.reply_text = AsyncMock()
        self.update.message.reply_photo = AsyncMock()

    async def test_unsupported_url_replies_error(self):
        """Unsupported URL should get URL_NOT_SUPPORTED reply."""
        self.update.message.text = "https://example.com/not-supported"
        await messages.on_message(self.update, self.context)
        self.update.message.reply_text.assert_awaited_once_with(Texts.URL_NOT_SUPPORTED)

    async def test_plain_text_replies_error(self):
        """Plain text (no URL) should get URL_NOT_SUPPORTED reply."""
        self.update.message.text = "just some random text"
        await messages.on_message(self.update, self.context)
        self.update.message.reply_text.assert_awaited_once_with(Texts.URL_NOT_SUPPORTED)

    async def test_empty_text_replies_error(self):
        """Empty text should get URL_NOT_SUPPORTED reply."""
        self.update.message.text = ""
        await messages.on_message(self.update, self.context)
        self.update.message.reply_text.assert_awaited_once_with(Texts.URL_NOT_SUPPORTED)

    async def test_rate_limited_user_replies_rate_limited(self):
        """Rate-limited user gets RATE_LIMITED reply."""
        self.update.message.text = "https://youtube.com/watch?v=abc"
        state.limiter.allow_user = AsyncMock(return_value=False)
        await messages.on_message(self.update, self.context)
        self.update.message.reply_text.assert_awaited_with(Texts.RATE_LIMITED)

    async def test_rate_limited_chat_replies_rate_limited(self):
        """Rate-limited chat gets RATE_LIMITED reply."""
        self.update.message.text = "https://youtube.com/watch?v=abc"
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=False)
        await messages.on_message(self.update, self.context)
        self.update.message.reply_text.assert_awaited_with(Texts.RATE_LIMITED)

    async def test_cached_url_shows_format_selection(self):
        """URL with cached info should show format keyboard without re-parsing."""
        url = "https://youtube.com/watch?v=cached123"
        self.update.message.text = url

        formats = [
            MagicMock(format_id="137", label="1080p", height=1080, filesize=50_000_000)
        ]
        special_format = MagicMock(
            format_id="bestaudio/best", label="Audio", filesize=None
        )

        from app.services.ytdlp.models import ExtractionResult

        # Pre-populate cache
        state.info_cache[url] = ExtractionResult(
            title="Cached Video Title",
            formats=formats,
            special_format=special_format,
            duration_str="5:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url=None,
            tiktok_auth_error=False,
        )

        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)

        with patch("app.bot.messages.build_format_keyboard") as mock_kb:
            mock_kb.return_value = MagicMock()
            await messages.on_message(self.update, self.context)

        # Should have called edit_text with the title (status message)
        self.assertTrue(status_msg.edit_text.called)
        args, kwargs = status_msg.edit_text.call_args
        self.assertIn("Cached Video Title", args[0])

        # user_data should have page_url set
        self.assertEqual(self.context.user_data["page_url"], url)
        self.assertEqual(self.context.user_data["title"], "Cached Video Title")

    async def test_uncached_list_formats_binds_parse_cancellation_owner(self):
        from app.core.process import current_process_owner
        from app.services.ytdlp.models import ExtractionResult

        url = "https://youtube.com/watch?v=owner123"
        self.update.message.text = url
        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)

        async def list_formats(_url):
            self.assertEqual(current_process_owner(), "parseown")
            return ExtractionResult(
                title="Owned parse",
                formats=[],
                special_format=None,
                duration_str="0:01",
                is_slideshow=False,
                info_json_path=None,
                thumbnail_url=None,
            )

        ytdlp = MagicMock()
        ytdlp.list_formats = AsyncMock(side_effect=list_formats)
        with (
            patch.object(state, "media_pipeline", None),
            patch.object(state, "ytdlp", ytdlp),
            patch.object(
                messages.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex="parseown"),
            ),
            patch.object(messages, "build_format_keyboard", return_value=MagicMock()),
        ):
            await messages.on_message(self.update, self.context)

        self.assertEqual(self.context.user_data["parse_token"], "parseown")

    async def test_tiktok_api_failure_assigns_synthetic_cache_value(self):
        """TikTok fallback must not read the local ``cached`` before assignment."""
        self.update.message.text = "https://www.tiktok.com/@user/video/123"
        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)

        with (
            patch.object(state, "media_pipeline", None),
            patch.object(messages, "ENABLE_COBALT_TIKTOK", False),
            patch.object(
                messages.TikWMService,
                "process",
                new=AsyncMock(side_effect=RuntimeError("TikWM unavailable")),
            ),
            patch.object(messages, "classify_tiktok_content", return_value="video"),
            patch.object(messages, "build_format_keyboard", return_value=MagicMock()),
        ):
            await messages.on_message(self.update, self.context)

        self.assertEqual(self.context.user_data["page_url"], self.update.message.text)
        self.assertEqual(self.context.user_data["title"], "TikTok Content")
        self.assertTrue(status_msg.edit_text.called)


if __name__ == "__main__":
    unittest.main()
