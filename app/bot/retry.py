"""Saved Telegram media requests and their owner-bound retry control."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, replace

import msgspec
from cachetools import TTLCache
from telegram import InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.bot.keyboards import build_retry_keyboard
from app.core import state
from app.core.config import LINK_TTL_MINUTES
from app.core.job_store import (
    DeliveryOutcome,
    JobStore,
    current_delivery_job_id,
    current_job_can_retry,
    mark_current_job_failed,
    prepare_current_retry,
)
from app.core.models import DownloadContext
from app.core.process import process_owner_scope
from app.core.texts import Texts
from app.services.media.models import (
    DeliveryReceipt,
    DeliveryStatus,
    DeliveryTarget,
    MediaKind,
    MediaRequest,
)
from app.services.media.pipeline import (
    CallbackDataError,
    MediaPipelineError,
    decode_callback_payload,
    encode_callback_data,
)

logger = logging.getLogger(__name__)
_active_retries: set[str] = set()
_finished_retries: TTLCache[str, bool] = TTLCache(
    maxsize=1000, ttl=LINK_TTL_MINUTES * 60
)


@dataclass(frozen=True)
class RetryRequest:
    request: MediaRequest
    user_id: int
    chat_id: int
    message_id: int
    caption: str
    source_message_id: int | None = None
    job_id: str | None = None
    fallback_markup: dict | None = None


async def save_retry_request(
    request: MediaRequest,
    *,
    user_id: int,
    chat_id: int,
    message_id: int,
    caption: str,
    source_message_id: int | None = None,
    token: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    """Persist the exact request before publishing a credential-free callback."""
    token = token or uuid.uuid4().hex
    saved = RetryRequest(
        replace(request, deadline=None),
        user_id,
        chat_id,
        message_id,
        caption,
        source_message_id,
        current_delivery_job_id(),
        reply_markup.to_dict() if reply_markup is not None else None,
    )
    await state.link_cache.set(f"retry:{token}", asdict(saved))
    rows = list(build_retry_keyboard(token).inline_keyboard)
    if reply_markup is not None:
        rows.extend(reply_markup.inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def can_retry_error(error: MediaPipelineError) -> bool:
    return error.retryable and await current_job_can_retry()


def _retry_markup(saved: RetryRequest, token: str) -> InlineKeyboardMarkup:
    rows = list(build_retry_keyboard(token).inline_keyboard)
    if saved.fallback_markup is not None:
        fallback = InlineKeyboardMarkup.de_json(saved.fallback_markup, None)
        if fallback is not None:
            rows.extend(fallback.inline_keyboard)
    return InlineKeyboardMarkup(rows)


async def _finish_retry(
    token: str,
    polling_store: JobStore | None = None,
    *,
    outcome: DeliveryOutcome = DeliveryOutcome.SUCCESS,
) -> None:
    # Completion must survive a transient cache cleanup error in this process.
    # Durable callbacks also refuse a predecessor already completed in SQLite.
    _finished_retries[token] = True
    if polling_store is not None:
        await polling_store.record_delivery(f"retry:{token}", outcome)
    try:
        await state.link_cache.delete(f"retry:{token}")
    # Cache backends are extensible; cleanup cannot undo durable/in-process
    # completion, and the diagnostic deliberately exposes only the error type.
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "retry cache cleanup failed", extra={"error_type": type(error).__name__}
        )


def can_retry_delivery(receipt: DeliveryReceipt) -> bool:
    """Repeating a whole request is safe only when nothing was sent or uncertain."""
    return receipt.status is DeliveryStatus.FAILED and all(
        item.status is DeliveryStatus.FAILED for item in receipt.items
    )


async def _offer_slideshow(saved: RetryRequest, status: Message) -> bool:
    """A retried TikTok discovery retains the original album/video choice."""
    request = saved.request
    pipeline = state.media_pipeline
    assert pipeline is not None
    if request.platform != "tiktok" or request.kind is not MediaKind.AUTO:
        return False
    count = await pipeline.cached_item_count(request)
    items = (await pipeline.resolve(request)).items if count is None else ()
    if not (
        (count is not None and count > 1)
        or (len(items) > 1 and all(item.kind is MediaKind.PHOTO for item in items))
    ):
        return False
    token = uuid.uuid4().hex
    interval = request.clip
    section = None
    if interval.start_seconds is not None or interval.end_seconds is not None:
        section = f"*{interval.start_seconds or 0}-{interval.end_seconds if interval.end_seconds is not None else ''}"
    await state.link_cache.set(
        token,
        DownloadContext(
            page_url=request.canonical_url,
            user_tag=saved.caption.removeprefix("📹 "),
            chat_id=saved.chat_id,
            original_msg_id=saved.source_message_id or status.message_id,
            api_source="pipeline",
            api_json={
                "media_id": request.media_id,
                "item_count": count or len(items),
                "item_kinds": [item.kind.value for item in items],
            },
            section=section,
        ),
    )
    from telegram import InlineKeyboardButton

    from app.bot.keyboards import build_slideshow_keyboard

    if request.caller_scope == "group":
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📸 Альбом",
                        callback_data=encode_callback_data(
                            "apigrpslide", token, "photo"
                        ),
                    ),
                    InlineKeyboardButton(
                        "🎬 Видео",
                        callback_data=encode_callback_data(
                            "apigrpslide", token, "video"
                        ),
                    ),
                ]
            ]
        )
    else:
        markup = build_slideshow_keyboard(token)
    await status.edit_text(Texts.GROUP_SLIDESHOW_CHOICE, reply_markup=markup)
    return True


async def on_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None
    if not isinstance(query.message, Message) or query.from_user is None:
        await query.answer(Texts.LINK_EXPIRED, show_alert=True)
        return
    try:
        _, (token,) = decode_callback_payload(
            query.data or "", allowed_actions=("retry",), max_parts=1
        )
        if token in _finished_retries:
            await query.answer(Texts.LINK_EXPIRED, show_alert=True)
            return
        raw = await state.link_cache.get(f"retry:{token}")
        saved = msgspec.convert(raw, type=RetryRequest)
    except (CallbackDataError, ValueError, TypeError, msgspec.ValidationError):
        await query.answer(Texts.LINK_EXPIRED, show_alert=True)
        return
    status = query.message
    if (saved.user_id, saved.chat_id, saved.message_id) != (
        query.from_user.id,
        status.chat_id,
        status.message_id,
    ):
        await query.answer(Texts.RETRY_NOT_OWNER, show_alert=True)
        return
    # A concurrent cache GET may return an old snapshot after another retry
    # has completed. Check again without yielding before claiming the token.
    if token in _finished_retries:
        await query.answer(Texts.LINK_EXPIRED, show_alert=True)
        return
    if token in _active_retries:
        await query.answer(Texts.RETRY_IN_PROGRESS)
        return
    _active_retries.add(token)
    try:
        if not await state.limiter.allow_user(
            saved.user_id
        ) or not await state.limiter.allow_chat(saved.chat_id):
            await query.answer(Texts.RATE_LIMITED, show_alert=True)
            return
        pipeline = state.media_pipeline
        if pipeline is None:
            await query.answer(Texts.RETRY_UNAVAILABLE, show_alert=True)
            return
        if not await prepare_current_retry(saved.job_id):
            await query.answer(Texts.LINK_EXPIRED, show_alert=True)
            return
        polling_store = None
        if current_delivery_job_id() is None:
            polling_store = state.job_store
            if polling_store is None:
                await query.answer(Texts.RETRY_UNAVAILABLE, show_alert=True)
                return
            if await polling_store.delivery_outcome(f"retry:{token}") not in {
                None,
                DeliveryOutcome.FAILED,
            }:
                await query.answer(Texts.LINK_EXPIRED, show_alert=True)
                return
        saved = replace(saved, job_id=current_delivery_job_id() or saved.job_id)
        await state.link_cache.set(f"retry:{token}", asdict(saved))
        await query.answer(Texts.DOWNLOAD_STARTED)
        await status.edit_text(Texts.SEARCHING, reply_markup=None)
        try:
            # Reserve only once UI setup has succeeded. A failed acknowledgement
            # or edit cannot have sent media and must leave the button usable.
            if polling_store is not None and not await polling_store.begin_retry_token(
                token
            ):
                await status.edit_text(Texts.LINK_EXPIRED, reply_markup=None)
                return
            with process_owner_scope(token):
                if await _offer_slideshow(saved, status):
                    await _finish_retry(token, polling_store)
                    return
                receipt = await pipeline.deliver(
                    saved.request,
                    DeliveryTarget(
                        str(saved.chat_id),
                        caller_scope=saved.request.caller_scope,
                        auth_scope=saved.request.auth_scope,
                    ),
                    caption=saved.caption,
                )
        except MediaPipelineError as error:
            mark_current_job_failed(error)
            logger.warning(
                "media retry failed",
                extra={"op": "media-retry-failed", "error": str(error)},
            )
            markup = (
                _retry_markup(saved, token) if await can_retry_error(error) else None
            )
            if markup is None:
                await _finish_retry(
                    token, polling_store, outcome=DeliveryOutcome.UNCERTAIN
                )
            elif polling_store is not None:
                await polling_store.record_delivery(
                    f"retry:{token}", DeliveryOutcome.FAILED
                )
            await status.edit_text(str(error), reply_markup=markup)
            return
        if not receipt.success:
            text = next(
                (item.error for item in receipt.items if item.error), Texts.SEND_ERROR
            )
            markup = (
                _retry_markup(saved, token) if can_retry_delivery(receipt) else None
            )
            if markup is None:
                await _finish_retry(
                    token, polling_store, outcome=DeliveryOutcome.UNCERTAIN
                )
            elif polling_store is not None:
                await polling_store.record_delivery(
                    f"retry:{token}", DeliveryOutcome.FAILED
                )
            await status.edit_text(text, reply_markup=markup)
            return
        await _finish_retry(token, polling_store)
        try:
            await status.delete()
        # The retry is finalized before optional Telegram UI cleanup.
        except Exception:  # noqa: BLE001, S110
            pass
        if saved.source_message_id is not None:
            try:
                await context.bot.delete_message(
                    chat_id=saved.chat_id, message_id=saved.source_message_id
                )
            # Deleting the original UI must not invalidate completed delivery.
            except Exception:  # noqa: BLE001, S110
                pass
    finally:
        _active_retries.discard(token)
