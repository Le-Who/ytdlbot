from unittest.mock import AsyncMock

"""Tests for app.bot.group_logic — handle_group_message + on_group_slideshow."""

import io
import unittest


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


from unittest.mock import MagicMock, patch

from app.core import state
from app.core.texts import Texts


class TestHandleGroupMessage(unittest.IsolatedAsyncioTestCase):
    """Test handle_group_message."""

    async def asyncSetUp(self):
        import asyncio

        state.limiter = MagicMock()
        state.limiter.allow_user = AsyncMock(return_value=True)
        state.limiter.allow_chat = AsyncMock(return_value=True)
        state.link_cache = AsyncMockCache()
        state.file_cache = AsyncMockCache()
        state.cancel_cache = AsyncMockCache()
        state.download_sem = asyncio.Semaphore(5)
        state.api_sem = asyncio.Semaphore(10)
        from app.core.download_queue import DownloadQueue

        state.download_queue = DownloadQueue(state.download_sem, max_queue_size=15)
        state.api_queue = DownloadQueue(state.api_sem, max_queue_size=15)
        state.disk_critical = False

        self.context = MagicMock()
        self.context.bot = AsyncMock()

        self.update = MagicMock()
        self.update.effective_user.id = 123
        self.update.effective_user.username = "testuser"
        self.update.effective_user.mention_html.return_value = "<a>testuser</a>"
        self.update.effective_chat.id = 456
        self.update.effective_chat.type = "group"
        self.update.message.message_id = 1
        self.update.message.reply_text = AsyncMock()
        self.update.message.delete = AsyncMock()

    async def test_no_message_returns_silently(self):
        """No message → return immediately (passive mode)."""
        from app.bot.group_logic import handle_group_message

        self.update.message = None
        await handle_group_message(self.update, self.context)
        # No reply, no crash

    async def test_no_text_returns_silently(self):
        """Message without text → return immediately."""
        from app.bot.group_logic import handle_group_message

        self.update.message.text = None
        await handle_group_message(self.update, self.context)

    async def test_unsupported_url_returns_silently(self):
        """Unsupported URL → no reply in group (passive mode)."""
        from app.bot.group_logic import handle_group_message

        self.update.message.text = "https://example.com/not-supported"
        await handle_group_message(self.update, self.context)
        self.update.message.reply_text.assert_not_awaited()

    async def test_plain_text_returns_silently(self):
        """Plain text → no reply in group (passive mode)."""
        from app.bot.group_logic import handle_group_message

        self.update.message.text = "just chatting"
        await handle_group_message(self.update, self.context)
        self.update.message.reply_text.assert_not_awaited()

    async def test_rate_limited_user_returns_silently(self):
        """Rate-limited user → no reply, no download."""
        from app.bot.group_logic import handle_group_message

        state.limiter.allow_user = AsyncMock(return_value=False)
        self.update.message.text = "https://youtube.com/watch?v=abc"
        await handle_group_message(self.update, self.context)
        self.update.message.reply_text.assert_not_awaited()

    async def test_rate_limited_chat_returns_silently(self):
        """Rate-limited chat → no reply, no download."""
        from app.bot.group_logic import handle_group_message

        state.limiter.allow_chat = AsyncMock(return_value=False)
        self.update.message.text = "https://youtube.com/watch?v=abc"
        await handle_group_message(self.update, self.context)
        self.update.message.reply_text.assert_not_awaited()

    @patch("app.bot.group_logic.MediaSender")
    async def test_download_error_shows_error(self, mock_sender):
        """Download failure shows error message."""
        from app.bot.group_logic import handle_group_message

        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)

        mock_sender.download_video = AsyncMock(return_value=(None, "⚠️ Ошибка загрузки"))

        state.ytdlp = AsyncMock()
        state.ytdlp.tiktok_proxy = None
        self.update.message.text = "https://youtube.com/watch?v=abc"
        await handle_group_message(self.update, self.context)
        status_msg.edit_text.assert_awaited()

    @patch("app.bot.group_logic.MediaSender")
    async def test_successful_download_sends_file(self, mock_sender):
        """Successful download sends file and deletes original message."""
        from app.bot.group_logic import handle_group_message

        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)

        mock_sender.download_video = AsyncMock(return_value=("/tmp/video.mp4", None))
        mock_sender.send_file = AsyncMock(return_value=True)

        state.ytdlp = MagicMock()
        state.ytdlp.tiktok_proxy = None
        state.ytdlp_executor = None

        from app.services.ytdlp.models import ExtractionResult

        mock_result = ExtractionResult(
            title="Video",
            formats=[],
            special_format=MagicMock(),
            duration_str="1:00",
            is_slideshow=False,
            info_json_path=None,
            thumbnail_url=None,
            tiktok_auth_error=False,
        )
        state.ytdlp.list_formats = MagicMock(return_value=mock_result)

        self.update.message.text = "https://youtube.com/watch?v=abc"
        await handle_group_message(self.update, self.context)

        mock_sender.send_file.assert_awaited_once()
        self.update.message.delete.assert_awaited_once()

    @patch("app.bot.group_logic.MediaSender")
    @patch("app.services.tikwm.TikWMService.process", new_callable=AsyncMock)
    async def test_tiktok_photo_url_shows_slideshow_choice(
        self, mock_tikwm_process, mock_sender
    ):
        """TikTok /photo/ URL shows slideshow format choice."""
        from app.bot.group_logic import handle_group_message

        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)
        state.ytdlp = MagicMock()
        state.ytdlp.tiktok_proxy = None
        state.ytdlp_executor = None

        from app.services.tikwm import TikWMResult

        mock_tikwm_process.return_value = TikWMResult(
            status="picker",
            url=None,
            images=["http://example.com"],
            audio_url="test",
        )

        state.ytdlp.list_formats = MagicMock()

        self.update.message.text = "https://tiktok.com/@user/photo/123"

        await handle_group_message(self.update, self.context)

        # Should show slideshow choice, not download
        args, kwargs = status_msg.edit_text.call_args
        self.assertEqual(args[0], Texts.GROUP_SLIDESHOW_CHOICE)
        self.assertIn("reply_markup", kwargs)

    async def test_tiktok_fallback_buffer_is_delivered_without_path_processing_or_cache(
        self,
    ):
        from app.bot import group_logic
        from app.services.tikwm import TikWMResult, TikWMService

        url = "https://www.tiktok.com/@user/video/123"
        self.update.message.text = url
        self.update.message.caption = None
        self.update.message.reply_to_message = None
        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)
        buffer = io.BytesIO(b"synthetic fallback video")
        file_cache = AsyncMockCache()
        with (
            patch.object(state, "media_pipeline", None),
            patch.object(state, "file_cache", file_cache),
            patch.object(
                TikWMService,
                "process",
                new=AsyncMock(
                    return_value=TikWMResult(
                        status="video", url="https://synthetic.invalid/video.mp4"
                    )
                ),
            ),
            patch.object(
                TikWMService,
                "download_video",
                new=AsyncMock(return_value=(None, "synthetic TikWM download failure")),
            ) as primary,
            patch.object(
                group_logic.MediaSender,
                "download_video",
                new=AsyncMock(return_value=(buffer, None)),
            ) as fallback,
            patch.object(
                group_logic.MediaSender, "send_file", new=AsyncMock(return_value=True)
            ) as sender,
            patch(
                "app.services.orchestrator.extract_video_meta",
                new=AsyncMock(side_effect=AssertionError("buffer must not be probed")),
            ) as probe,
            patch(
                "app.services.orchestrator.ensure_telegram_compatible",
                new=AsyncMock(
                    side_effect=AssertionError("buffer must not be converted")
                ),
            ) as convert,
        ):
            await group_logic.handle_group_message(self.update, self.context)

        primary.assert_awaited_once()
        fallback.assert_awaited_once()
        sender.assert_awaited_once()
        self.assertIs(sender.await_args.args[2], buffer)
        self.assertFalse(buffer.closed)
        probe.assert_not_awaited()
        convert.assert_not_awaited()
        self.assertEqual(file_cache, {})


class TestOnGroupSlideshow(unittest.IsolatedAsyncioTestCase):
    """Test on_group_slideshow callback."""

    async def asyncSetUp(self):
        import asyncio

        state.link_cache = AsyncMockCache()
        state.file_cache = AsyncMockCache()
        state.download_sem = asyncio.Semaphore(5)
        state.api_sem = asyncio.Semaphore(10)
        from app.core.download_queue import DownloadQueue

        state.download_queue = DownloadQueue(state.download_sem, max_queue_size=15)
        state.api_queue = DownloadQueue(state.api_sem, max_queue_size=15)
        state.disk_critical = False

    async def test_no_data_returns(self):
        """No callback data → immediate return."""
        from app.bot.group_logic import on_group_slideshow

        q = AsyncMock()
        q.data = None
        update = MagicMock()
        update.callback_query = q

        await on_group_slideshow(update, MagicMock())
        q.answer.assert_awaited_once()

    async def test_missing_token_shows_error(self):
        """Expired token shows error."""
        from app.bot.group_logic import on_group_slideshow

        q = AsyncMock()
        q.data = "grpslide|expired_token|photo"
        update = MagicMock()
        update.callback_query = q

        await on_group_slideshow(update, MagicMock())
        q.edit_message_text.assert_awaited_with(Texts.SLIDESHOW_ERROR)

    async def test_malformed_data_returns(self):
        """Malformed callback data → immediate return."""
        from app.bot.group_logic import on_group_slideshow

        q = AsyncMock()
        q.data = "invalid_data"
        update = MagicMock()
        update.callback_query = q

        await on_group_slideshow(update, MagicMock())
        q.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
