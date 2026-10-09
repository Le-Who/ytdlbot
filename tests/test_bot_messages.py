from unittest.mock import AsyncMock

"""Tests for on_message handler — the core user flow."""

import asyncio
import hashlib
import io
import sqlite3
import unittest
from types import SimpleNamespace

import pytest


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
        args, _kwargs = status_msg.edit_text.call_args
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

    async def test_cancel_parse_stops_hanging_search(self):
        from app.bot import callbacks

        url = "https://youtube.com/watch?v=cancelparse"
        self.update.message.text = url
        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)
        search_started = asyncio.Event()
        search_closed = asyncio.Event()

        async def list_formats(_url):
            search_started.set()
            try:
                await asyncio.Future()
            finally:
                search_closed.set()

        ytdlp = MagicMock()
        ytdlp.list_formats = AsyncMock(side_effect=list_formats)
        cancel_update = MagicMock()
        cancel_update.callback_query = MagicMock()
        cancel_update.callback_query.data = "cancel_parse|parseown"
        cancel_update.callback_query.answer = AsyncMock()
        cancel_update.callback_query.edit_message_text = AsyncMock()
        message_task = None

        try:
            with (
                patch.object(state, "media_pipeline", None),
                patch.object(state, "ytdlp", ytdlp),
                patch.object(
                    messages.uuid,
                    "uuid4",
                    return_value=SimpleNamespace(hex="parseown"),
                ),
            ):
                message_task = asyncio.create_task(
                    messages.on_message(self.update, self.context)
                )
                await asyncio.wait_for(search_started.wait(), timeout=1)

                await callbacks.on_cancel(cancel_update, self.context)

                self.assertTrue(message_task.done())
                with self.assertRaises(asyncio.CancelledError):
                    await message_task
                await asyncio.wait_for(search_closed.wait(), timeout=1)
                self.assertNotIn(url, state.inflight_parsing)
        finally:
            if message_task is not None and not message_task.done():
                message_task.cancel()
                await asyncio.gather(message_task, return_exceptions=True)

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

    async def test_tiktok_fallback_buffer_is_delivered_without_path_processing_or_cache(
        self,
    ):
        from app.services.downloader import MediaSender
        from app.services.sender import TelegramSender
        from app.services.tikwm import TikWMResult

        url = "https://www.tiktok.com/@user/video/123"
        self.update.message.text = url
        status_msg = AsyncMock()
        self.update.message.reply_text = AsyncMock(return_value=status_msg)
        self.update.message.delete = AsyncMock()
        buffer = io.BytesIO(b"synthetic fallback video")
        file_cache = AsyncMockCache()
        with (
            patch.object(state, "media_pipeline", None),
            patch.object(state, "file_cache", file_cache),
            patch.object(
                messages.TikWMService,
                "process",
                new=AsyncMock(
                    return_value=TikWMResult(
                        status="video", url="https://synthetic.invalid/video.mp4"
                    )
                ),
            ),
            patch.object(
                messages.TikWMService,
                "download_video",
                new=AsyncMock(return_value=(None, "synthetic TikWM download failure")),
            ) as primary,
            patch.object(
                MediaSender,
                "download_video",
                new=AsyncMock(return_value=(buffer, None)),
            ) as fallback,
            patch.object(
                TelegramSender, "send_file", new=AsyncMock(return_value=True)
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
            await messages.on_message(self.update, self.context)

        primary.assert_awaited_once()
        fallback.assert_awaited_once()
        sender.assert_awaited_once()
        self.assertIs(sender.await_args.args[2], buffer)
        self.assertFalse(buffer.closed)
        probe.assert_not_awaited()
        convert.assert_not_awaited()
        self.assertEqual(file_cache, {})


@pytest.mark.parametrize("mode", ["direct-url", "local-download", "picker-video"])
async def test_twitter_receipt_write_failure_propagates_without_fallback(
    tmp_path, monkeypatch, mode
):
    from app.core import config
    from app.core.job_store import DeliveryOutcome, JobStore, delivery_job_context
    from app.services.cobalt import CobaltPickerItem, CobaltResult

    class FailingFinalizationStore(JobStore):
        def _finalize_delivery_attempt_sync(self, *args, **kwargs):
            raise sqlite3.OperationalError("synthetic Twitter receipt write failure")

    url = "https://x.com/test/status/123"
    local_download = mode != "direct-url"
    source = tmp_path / "source.mp4"
    original = b"\x00\x00\x00\x18ftypisom"
    source.write_bytes(original)
    downloaded = tmp_path / "downloaded.mp4"
    if local_download:
        downloaded.write_bytes(original)
    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    msg = SimpleNamespace(reply_text=AsyncMock(return_value=status), delete=AsyncMock())
    update = SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(mention_html=lambda: "synthetic-user"),
        effective_chat=SimpleNamespace(id=42),
    )
    bot = SimpleNamespace(
        send_video=AsyncMock(
            return_value=SimpleNamespace(
                message_id=125, video=SimpleNamespace(file_id="twitter-fid")
            )
        )
    )
    monkeypatch.setattr(config, "TELEGRAM_LOCAL_ENDPOINT", "")
    monkeypatch.setattr(
        messages.CobaltService,
        "process",
        AsyncMock(
            return_value=CobaltResult(
                status="picker" if mode == "picker-video" else "redirect",
                url="https://video.twimg.com/synthetic.mp4",
                picker=[
                    CobaltPickerItem(
                        type="video", url="https://video.twimg.com/synthetic.mp4"
                    )
                ]
                if mode == "picker-video"
                else [],
            )
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestrator._cobalt_url_head_size",
        AsyncMock(return_value=None if local_download else 100),
    )
    download = AsyncMock(return_value=str(downloaded))
    monkeypatch.setattr(messages.CobaltService, "download_file", download)
    store = FailingFinalizationStore(tmp_path / "jobs.sqlite3")
    await store.accept_update({"update_id": 993})
    claimed = await store.claim_next("worker-a")
    assert claimed is not None

    with (
        delivery_job_context(store, claimed.id, owner_id="worker-a"),
        pytest.raises(
            sqlite3.OperationalError, match="synthetic Twitter receipt write failure"
        ),
    ):
        await messages._handle_twitter(
            update, SimpleNamespace(bot=bot), url, "synthetic", None
        )

    bot.send_video.assert_awaited_once()
    operation = f"x:{url}:item:0" if mode == "picker-video" else f"x:{url}:video"
    digest = hashlib.sha256(f"compat-file:video:{operation}".encode()).hexdigest()
    assert (
        await store.delivery_outcome(claimed.id, item_key=f"42:operation:{digest}:0")
        is DeliveryOutcome.UNCERTAIN
    )
    assert await store.has_unsafe_deliveries(claimed.id)
    assert not await store.has_failed_deliveries(claimed.id)
    # Failure after durable sending starts must not advertise a yt-dlp fallback.
    status.delete.assert_not_awaited()
    msg.delete.assert_not_awaited()
    if local_download:
        download.assert_awaited_once()
        assert not downloaded.exists()
    else:
        download.assert_not_awaited()
    assert source.read_bytes() == original


@pytest.mark.parametrize("provider_raises", [False, True])
async def test_twitter_provider_failure_before_sending_preserves_false_fallback(
    monkeypatch, provider_raises
):
    from app.services.cobalt import CobaltResult

    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    msg = SimpleNamespace(reply_text=AsyncMock(return_value=status), delete=AsyncMock())
    update = SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(mention_html=lambda: "synthetic-user"),
        effective_chat=SimpleNamespace(id=42),
    )
    bot = SimpleNamespace(send_video=AsyncMock())
    process = (
        AsyncMock(side_effect=RuntimeError("synthetic provider failure"))
        if provider_raises
        else AsyncMock(
            return_value=CobaltResult(
                status="error", error_message="synthetic provider failure"
            )
        )
    )
    monkeypatch.setattr(messages.CobaltService, "process", process)
    download = AsyncMock(
        side_effect=AssertionError("provider failure must not download")
    )
    head = AsyncMock(
        side_effect=AssertionError("provider failure must not inspect remote media")
    )
    monkeypatch.setattr(messages.CobaltService, "download_file", download)
    monkeypatch.setattr("app.services.orchestrator._cobalt_url_head_size", head)

    success = await messages._handle_twitter(
        update,
        SimpleNamespace(bot=bot),
        "https://x.com/test/status/123",
        "synthetic",
        None,
    )

    assert success is False
    bot.send_video.assert_not_awaited()
    download.assert_not_awaited()
    head.assert_not_awaited()
    status.delete.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
