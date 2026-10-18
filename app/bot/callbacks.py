import asyncio
import hashlib
import html
import logging
import os
import tempfile
import uuid
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.error import BadRequest, NetworkError
from telegram.ext import ContextTypes

from app.bot.keyboards import build_format_keyboard
from app.bot.retry import on_retry
from app.constants import (
    AUDIO_FORMAT_ID,
    GIF_FORMAT_ID,
    SLIDESHOW_PHOTO_FORMAT_ID,
    SLIDESHOW_VIDEO_FORMAT_ID,
)
from app.core import state
from app.core.config import (
    BASE_URL,
    ENABLE_TELEGRAM_UPLOAD,
    LINK_TTL_MINUTES,
    TEMP_DIR,
)
from app.core.job_store import (
    DeliveryOutcome,
    begin_current_delivery,
    current_delivery_outcome,
    current_job_can_retry,
    record_current_delivery,
)
from app.core.logging import set_correlation_id
from app.core.models import DownloadContext
from app.core.process import process_owner_scope, process_supervisor
from app.core.texts import Texts
from app.core.utils import (
    safe_remove,
)
from app.services.downloader import MediaSender
from app.services.media.models import DeliveryReceipt, DeliveryTarget
from app.services.media.pipeline import (
    CallbackDataError,
    MediaPipelineError,
    build_media_request,
    decode_callback_payload,
    encode_callback_data,
)
from app.services.orchestrator import DownloadOrchestrator

__all__ = [
    "on_back",
    "on_cancel",
    "on_convert_to_gif",
    "on_pick",
    "on_retry",
    "on_save_as_gif_file",
    "on_send",
    "on_slideshow",
]

logger = logging.getLogger("app.bot.callbacks")


def _gif_document_delivery_key(chat_id: int, cache_key: str) -> str:
    return f"gif-document:{chat_id}:{cache_key}"


def _gif_source_cache_key(message: Message, token: str) -> tuple[str, Any | None]:
    """Scope exports to their exact sent source and original request token."""
    source = message.animation or message.video
    file_id = getattr(source, "file_id", None)
    if not isinstance(file_id, str) or not file_id:
        return f"gifdoc:{token}", None
    unique_id = getattr(source, "file_unique_id", None)
    identity = unique_id if isinstance(unique_id, str) and unique_id else file_id
    digest = hashlib.sha256(f"{token}:{identity}".encode()).hexdigest()
    return f"gifdoc:v2:{digest}", source


async def _recover_telegram_gif_source(
    bot: Any, telegram_source: Any, owned_paths: list[str]
) -> str | None:
    """Track a unique source before its download can fail or be cancelled."""
    local_source = state.local_sent_media_source(telegram_source)
    if local_source is not None:
        return local_source
    tg_file = await bot.get_file(telegram_source.file_id)
    descriptor, path = tempfile.mkstemp(prefix="gif_src_", suffix=".mp4", dir=TEMP_DIR)
    os.close(descriptor)
    owned_paths.append(path)
    await tg_file.download_to_drive(path)
    return path if os.path.getsize(path) > 0 else None


async def _send_gif_document_durable(
    bot: Any,
    item_key: str,
    **kwargs: Any,
) -> tuple[DeliveryOutcome, Any | None]:
    prior = await current_delivery_outcome(item_key)
    if prior in {DeliveryOutcome.SUCCESS, DeliveryOutcome.UNCERTAIN}:
        return prior, None
    await begin_current_delivery((item_key,))
    try:
        sent = await bot.send_document(**kwargs)
    except BadRequest:
        await record_current_delivery(item_key, DeliveryOutcome.FAILED)
        return DeliveryOutcome.FAILED, None
    except NetworkError:
        await record_current_delivery(item_key, DeliveryOutcome.UNCERTAIN)
        return DeliveryOutcome.UNCERTAIN, None
    # Bot transport extensions can fail outside Telegram errors; persist the failed receipt.
    except Exception:  # noqa: BLE001
        await record_current_delivery(item_key, DeliveryOutcome.FAILED)
        return DeliveryOutcome.FAILED, None
    message_id = getattr(sent, "message_id", None)
    await record_current_delivery(
        item_key,
        DeliveryOutcome.SUCCESS,
        delivery_id=str(message_id) if isinstance(message_id, int) else None,
    )
    return DeliveryOutcome.SUCCESS, sent


async def _edit_or_reply(
    q: Any, text: str, reply_markup: Any = None, **kwargs: Any
) -> None:
    """Safely edit a message, using caption for media or text for text messages."""
    msg = q.message
    try:
        if (
            getattr(msg, "photo", None)
            or getattr(msg, "video", None)
            or getattr(msg, "animation", None)
            or getattr(msg, "document", None)
        ):
            await q.edit_message_caption(
                caption=text, reply_markup=reply_markup, **kwargs
            )
        else:
            await q.edit_message_text(text, reply_markup=reply_markup, **kwargs)
    # Editing optional Telegram UI must fall back even when a transport adapter fails.
    except Exception as e:  # noqa: BLE001
        logger.warning("_edit_or_reply failed (editing caption/text): %s", e)
        # Ultimate fallback
        try:
            await msg.delete()
            await msg.chat.send_message(text, reply_markup=reply_markup, **kwargs)
        # The final UI fallback is best effort; delivery callers retain their own result.
        except Exception as e2:  # noqa: BLE001
            logger.error("_edit_or_reply ultimate fallback failed: %s", e2)


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q is not None
    await q.answer()

    data = context.user_data
    assert data is not None
    page_url = data.get("page_url")
    if not page_url:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    cached = await state.info_cache.get(page_url)
    if not cached:
        try:
            await q.edit_message_text(Texts.CACHE_REFRESHING)
            async with state.parsing_sem:
                result = await state.ytdlp.list_formats(page_url)
            await state.info_cache.set(page_url, result)

            title = result.title
            formats = result.formats
            special_format = result.special_format
            duration = result.duration_str
            is_slideshow = result.is_slideshow
            thumbnail_url = result.thumbnail_url
        # Refresh spans provider, cache and Telegram APIs; report failure at this UI boundary.
        except Exception as e:  # noqa: BLE001
            logger.error("Refresh error on back", extra={"error": str(e)})
            await q.edit_message_text(Texts.CACHE_REFRESH_FAIL)
            return
    else:
        if isinstance(cached, dict):
            from app.services.ytdlp.models import ExtractionResult

            cached = ExtractionResult.from_dict(cached)
        title = cached.title
        formats = cached.formats
        special_format = cached.special_format
        duration = cached.duration_str
        is_slideshow = cached.is_slideshow
        thumbnail_url = cached.thumbnail_url

    if is_slideshow:
        from app.bot.keyboards import build_slideshow_keyboard

        reply_markup = build_slideshow_keyboard()
        await q.edit_message_text(
            Texts.SLIDESHOW_DETECTED.format(title=html.escape(title)),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        indexed_formats = formats[:8]
        fmt_index_map = {str(i): f.format_id for i, f in enumerate(indexed_formats)}
        if special_format:
            fmt_index_map[str(len(indexed_formats))] = special_format.format_id
        data["fmt_index_map"] = fmt_index_map

        reply_markup = build_format_keyboard(formats, special_format)
        caption = f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}"

        if thumbnail_url:
            try:
                await q.edit_message_media(
                    media=InputMediaPhoto(
                        media=thumbnail_url,
                        caption=caption,
                        parse_mode="HTML",
                    ),
                    reply_markup=reply_markup,
                )
            # A failed media edit falls back to the text menu, including transport adapter failures.
            except Exception:  # noqa: BLE001
                # Fallback: original message might be text-only
                await q.edit_message_text(
                    caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
        else:
            await q.edit_message_text(
                caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q is not None
    await q.answer(Texts.PREPARING_LINK)

    try:
        await q.edit_message_reply_markup(None)
    # Removing stale buttons is best effort and must not prevent format selection.
    except Exception:  # noqa: BLE001, S110
        pass

    if not q.data:
        return

    try:
        _, parts = decode_callback_payload(
            q.data, allowed_actions=("pick",), max_parts=1
        )
        (pick_key,) = parts
    except (CallbackDataError, ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_pick", extra={"error": str(e)})
        return

    data = context.user_data
    assert data is not None
    if not data.get("page_url"):
        await _edit_or_reply(q, Texts.DATA_EXPIRED_RESEND)
        return

    fmt_index_map = data.get("fmt_index_map", {})
    format_id = fmt_index_map.get(pick_key, pick_key)

    token = uuid.uuid4().hex
    await state.link_cache.set(
        token,
        DownloadContext(
            page_url=data["page_url"],
            format_id=format_id,
            height=data["format_map"].get(format_id),
            title=data["title"],
            info_json_path=data.get("info_json_path"),
            youtube_fallback=data.get("youtube_fallback", False),
            section=data.get("section"),
        ),
    )

    dl_link = f"{BASE_URL}/dl/{token}"

    kb = [[InlineKeyboardButton(Texts.BTN_DOWNLOAD_LINK, url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [
                InlineKeyboardButton(
                    Texts.BTN_SEND_TG,
                    callback_data=encode_callback_data("send", token),
                )
            ]
        )

    kb.append(
        [
            InlineKeyboardButton(
                Texts.BTN_BACK, callback_data=encode_callback_data("back")
            )
        ]
    )

    height = data["format_map"].get(format_id)
    filesize = data.get("size_map", {}).get(format_id)

    quality_parts = []
    if height:
        quality_parts.append(f"{height}p")
    elif format_id == AUDIO_FORMAT_ID:
        quality_parts.append("Audio")
    elif format_id == GIF_FORMAT_ID:
        quality_parts.append("GIF")

    if filesize:
        mb = filesize / (1024 * 1024)
        quality_parts.append(f"{mb:.1f} MB")

    quality_str = f" ({' • '.join(quality_parts)})" if quality_parts else ""

    await _edit_or_reply(
        q,
        Texts.READY_LINK.format(
            quality=quality_str, ttl=LINK_TTL_MINUTES, link=html.escape(dl_link)
        ),
        reply_markup=InlineKeyboardMarkup(kb),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q is not None
    await q.answer(Texts.CANCELLING)

    if not q.data:
        return

    try:
        _, parts = decode_callback_payload(
            q.data,
            allowed_actions=("cancel", "cancel_parse"),
            max_parts=1,
        )
        (token,) = parts
        await state.cancel_cache.set(token, True)
        await process_supervisor.cancel_owner(token)
        await q.edit_message_text(Texts.CANCELLED)
    except (CallbackDataError, ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_cancel", extra={"error": str(e)})


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q is not None and q.from_user is not None and isinstance(q.message, Message)
    await q.answer(Texts.DOWNLOAD_STARTED)
    user_id = q.from_user.id

    try:
        await q.edit_message_reply_markup(None)
    # Removing stale buttons is best effort and must not prevent the download.
    except Exception:  # noqa: BLE001, S110
        pass

    if not await state.limiter.allow_user(
        user_id
    ) or not await state.limiter.allow_chat(q.message.chat_id):
        await q.edit_message_text(Texts.TOO_MANY_REQUESTS)
        return

    if not q.data:
        return

    try:
        _, parts = decode_callback_payload(
            q.data, allowed_actions=("send",), max_parts=1
        )
        (token,) = parts
    except (CallbackDataError, ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_send", extra={"error": str(e)})
        return

    set_correlation_id(token)
    payload = await state.link_cache.get(token)
    if not payload:
        await _edit_or_reply(q, Texts.LINK_EXPIRED)
        return

    dl_link = f"{BASE_URL}/dl/{token}"
    kb_error = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(Texts.BTN_DOWNLOAD_LINK, url=dl_link)],
            [
                InlineKeyboardButton(
                    Texts.BTN_BACK, callback_data=encode_callback_data("back")
                )
            ],
        ]
    )

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    data = context.user_data
    assert data is not None
    fmt_size = data.get("size_map", {}).get(payload.format_id)

    async def update_progress_ui(text: str, markup: object | None = None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")  # type: ignore
        # Progress edits are optional; adapter failures must not abort the active download.
        except Exception as e:  # noqa: BLE001
            logger.warning("UI update failed", extra={"error": str(e)})

    async def retry_keyboard(request):
        from app.bot.retry import save_retry_request

        assert isinstance(q.message, Message)
        return await save_retry_request(
            request,
            user_id=user_id,
            chat_id=q.message.chat_id,
            message_id=q.message.message_id,
            caption="🎵" if request.kind.value == "audio" else "📹",
            reply_markup=kb_error,
        )

    success = await DownloadOrchestrator.process_download(
        token=token,
        chat_id=q.message.chat_id,
        bot=context.bot,
        payload=payload,
        fmt_size=fmt_size,
        update_ui=update_progress_ui,
        kb_error=kb_error,
        retry_keyboard=retry_keyboard,
    )

    if success:
        try:
            await q.delete_message()
        # Deleting the status message must not overturn a confirmed delivery receipt.
        except Exception:  # noqa: BLE001, S110
            pass


async def on_convert_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Send GIF' button press from Group Mode."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data is not None
    try:
        await q.answer(Texts.GIF_CONVERTING)
        await q.edit_message_reply_markup(None)
    # Callback toast/button feedback is optional; conversion must remain available.
    except Exception as e:  # noqa: BLE001
        logger.warning("Callback answer failed", extra={"error": str(e)})

    try:
        _, parts = decode_callback_payload(
            q.data, allowed_actions=("gif",), max_parts=1
        )
        (token,) = parts
    except (CallbackDataError, ValueError):
        return

    # This single-send callback must also honor receipts from earlier releases.
    if not await current_job_can_retry():
        return
    source_key, telegram_source = _gif_source_cache_key(q.message, token)
    debounce_key = f"animation:{source_key}"
    if debounce_key in state.processing_gifs:
        try:
            await q.message.reply_text(Texts.GIF_ALREADY_IN_PROGRESS, do_quote=True)
        except Exception:  # noqa: BLE001, S110 - optional progress feedback
            pass
        return
    state.processing_gifs.add(debounce_key)
    owned_paths: list[str] = []
    try:
        video_path: str | None = None
        if telegram_source is None:
            video_path = state.file_cache.get(token)
        else:
            try:
                video_path = await _recover_telegram_gif_source(
                    context.bot, telegram_source, owned_paths
                )
            except Exception as error:  # noqa: BLE001 - optional source recovery
                logger.warning(
                    "Animation source recovery failed",
                    extra={"error_type": type(error).__name__},
                )
        if not video_path or not os.path.exists(video_path):
            try:
                await q.message.reply_text(Texts.GIF_FILE_EXPIRED, do_quote=True)
            except Exception:  # noqa: BLE001, S110 - optional expiry feedback
                pass
            return

        with process_owner_scope(token):
            gif_path = await MediaSender.convert_to_gif_ffmpeg(video_path)
        if gif_path and gif_path != video_path:
            owned_paths.append(gif_path)
        if not gif_path:
            try:
                await q.message.reply_text(Texts.GIF_CONVERSION_ERROR, do_quote=True)
            except Exception:  # noqa: BLE001, S110 - optional failure feedback
                pass
            return

        from app.bot.keyboards import build_sent_gif_keyboard

        receipt = await MediaSender.send_file(
            context.bot,
            q.message.chat_id,
            gif_path,
            is_gif=True,
            reply_to_message_id=q.message.message_id,
            caption="🎬 GIF",
            reply_markup=build_sent_gif_keyboard(token),
            operation_key=f"callback-gif:{source_key}",
        )
        if (
            isinstance(receipt, DeliveryReceipt)
            and receipt.success
            and len(receipt.items) == 1
            and gif_path != video_path
            and gif_path in owned_paths
            and state.local_sent_media_source(receipt.items[0])
            == os.path.realpath(gif_path)
        ):
            # The native-GIF button needs this exact confirmed animation. Give
            # its generated file a unique bounded cache owner before relinquishing it.
            state.file_cache[f"animation-source:{uuid.uuid4().hex}"] = gif_path
            owned_paths.remove(gif_path)
        if not receipt:
            try:
                await q.message.reply_text(Texts.GIF_SEND_ERROR, do_quote=True)
            except Exception:  # noqa: BLE001, S110 - optional failure feedback
                pass
    finally:
        state.processing_gifs.discard(debounce_key)
        for owned_path in reversed(owned_paths):
            safe_remove(owned_path)


async def on_slideshow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles slideshow button presses (photos or video)."""
    q = update.callback_query
    assert q is not None and q.from_user is not None and isinstance(q.message, Message)
    await q.answer(Texts.SLIDESHOW_DOWNLOADING)
    user_id = q.from_user.id

    try:
        await q.edit_message_reply_markup(None)
    # Removing stale buttons is optional; slideshow queue ownership must proceed.
    except Exception:  # noqa: BLE001, S110
        pass

    if not await state.limiter.allow_user(
        user_id
    ) or not await state.limiter.allow_chat(q.message.chat_id):
        await _edit_or_reply(q, Texts.TOO_MANY_REQUESTS)
        return

    if not q.data:
        return

    try:
        prefix, parts = decode_callback_payload(
            q.data,
            allowed_actions=("slideshow", "cbslide", "apislide"),
            max_parts=2,
        )
    except (CallbackDataError, ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_slideshow", extra={"error": str(e)})
        return

    is_api = prefix == "apislide"
    process_token: str | None = None

    if is_api:
        try:
            parse_token, mode = parts
        except ValueError:
            return

        process_token = parse_token
        payload = await state.link_cache.get(parse_token)
        if not payload:
            await _edit_or_reply(q, Texts.LINK_EXPIRED)
            return

        if isinstance(payload, dict):
            payload = DownloadContext(**payload)

        if not payload.api_json:
            await _edit_or_reply(q, Texts.LINK_EXPIRED)
            return

        api_source = payload.api_source
        if api_source == "tikwm":
            from app.services.tikwm import TikWMResult

            tikwm_result = TikWMResult(**payload.api_json)
        elif api_source == "cobalt":
            from app.services.cobalt import CobaltResult

            cobalt_result = CobaltResult(**payload.api_json)
        elif api_source != "pipeline":
            await _edit_or_reply(q, "⚠️ Неизвестный API источник.")
            return

        page_url = payload.page_url
    else:
        try:
            (mode,) = parts
        except ValueError:
            return
        data = context.user_data
        assert data is not None
        process_token = data.get("parse_token")
        page_url = data.get("page_url")
        if not page_url:
            await _edit_or_reply(q, Texts.DATA_EXPIRED_RESEND)
            return
    if is_api:
        if mode not in {"photo", "video"}:
            return
        is_photo_mode = mode == "photo"
    else:
        if mode not in {SLIDESHOW_PHOTO_FORMAT_ID, SLIDESHOW_VIDEO_FORMAT_ID}:
            return
        is_photo_mode = mode == SLIDESHOW_PHOTO_FORMAT_ID

    async def _queue_ui(text: str, _markup: object | None = None) -> None:
        await _edit_or_reply(q, text)

    acquired = await state.download_queue.enqueue(_queue_ui)
    if not acquired:
        return

    try:
        await _edit_or_reply(q, Texts.SLIDESHOW_DOWNLOADING)

        if is_api and api_source == "pipeline":
            pipeline = state.media_pipeline
            if pipeline is None:
                await _edit_or_reply(q, Texts.LINK_EXPIRED)
                return
            request = build_media_request(
                page_url,
                kind="auto",
                clip=payload.section,
                caller_scope="callback",
                exact=False,
            )
            if payload.api_json.get("media_id") != request.media_id:
                await _edit_or_reply(q, Texts.LINK_EXPIRED)
                return
            try:
                if is_photo_mode:
                    with process_owner_scope(process_token):
                        receipt = await pipeline.deliver(
                            request,
                            DeliveryTarget(
                                str(q.message.chat_id), caller_scope="callback"
                            ),
                            caption="📸",
                        )
                    pipeline_success = receipt.success
                    error_text = next(
                        (item.error for item in receipt.items if item.error),
                        Texts.SEND_ERROR,
                    )
                else:
                    with process_owner_scope(process_token):
                        receipt = await pipeline.deliver_slideshow_video(
                            request,
                            DeliveryTarget(
                                str(q.message.chat_id), caller_scope="callback"
                            ),
                            caption="🎬",
                        )
                    pipeline_success = receipt.success
                    error_text = next(
                        (item.error for item in receipt.items if item.error),
                        Texts.SEND_ERROR,
                    )
            except MediaPipelineError as pipeline_error:
                await _edit_or_reply(q, str(pipeline_error))
                return
            if pipeline_success:
                await q.delete_message()
            else:
                await _edit_or_reply(q, error_text)
            return

        if is_api:
            from app.services.gallery_dl.service import SlideshowResult

            image_paths: list[str] | None = []
            audio_path: str | None = None

            if api_source == "tikwm":
                from app.services.tikwm import TikWMService

                image_paths, audio_path = await TikWMService.download_slideshow(
                    tikwm_result
                )
            elif api_source == "cobalt":
                from app.services.cobalt import CobaltService

                image_paths, audio_path = await CobaltService.download_slideshow(
                    cobalt_result
                )

            if image_paths:
                result = SlideshowResult(images=image_paths, audio=audio_path)
                error = None
            else:
                result = None
                error = "⚠️ Ошибка загрузки слайдшоу из внешнего API."
        else:
            with process_owner_scope(process_token):
                result, error = await MediaSender.download_slideshow(page_url)

        if error or not result:
            await _edit_or_reply(q, error or Texts.SLIDESHOW_ERROR)
            return

        try:
            if is_photo_mode:
                # Send as photo album
                await _edit_or_reply(q, Texts.SLIDESHOW_SENDING)

                total = len(result.images)
                import math

                from app.services.sender import MAX_TELEGRAM_ALBUM_SIZE

                total_batches = math.ceil(total / MAX_TELEGRAM_ALBUM_SIZE)
                if total_batches > 1:
                    caption = Texts.SLIDESHOW_MULTI_ALBUM.format(
                        total_batches=total_batches, total=total
                    )
                else:
                    caption = "📸"

                receipt = await MediaSender.send_slideshow_photos(
                    context.bot,
                    q.message.chat_id,
                    result.images,
                    caption=caption,
                    operation_key=f"callback-slideshow:{page_url}:{mode}:photos",
                )

                if receipt:
                    await q.delete_message()
                else:
                    await _edit_or_reply(q, Texts.SEND_ERROR)
            else:
                # Convert to video and send
                await _edit_or_reply(q, Texts.SLIDESHOW_CONVERTING)

                with process_owner_scope(process_token):
                    video_path = await MediaSender.images_to_video(
                        result.images, result.audio
                    )

                if not video_path:
                    await _edit_or_reply(q, Texts.SLIDESHOW_ERROR)
                    return

                await _edit_or_reply(q, Texts.SENDING_TO_TG)

                receipt = await MediaSender.send_file(
                    context.bot,
                    q.message.chat_id,
                    video_path,
                    caption="🎬",
                    operation_key=f"callback-slideshow:{page_url}:{mode}:video",
                )

                if receipt:
                    await q.delete_message()
                else:
                    await _edit_or_reply(q, Texts.SEND_ERROR)

                # Cleanup video file
                await asyncio.to_thread(safe_remove, video_path)
        finally:
            # Always cleanup slideshow download directory
            await asyncio.to_thread(MediaSender.cleanup_slideshow, result)
    finally:
        state.download_queue.release()


async def on_save_as_gif_file(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles 'Save as .gif file' button press.

    On-demand native GIF export pipeline:
    1. Immediate Toast feedback + spinner button state.
    2. Check for an export of this exact Telegram source and request token.
    3. Recover the message's actual Telegram media, or its legacy local source.
    4. Convert to palettized .gif via convert_to_native_gif().
    5. Send as sendDocument (reply to the animation) — routes via Local API if configured.
    6. Cache the document.file_id in gifdoc_cache for the same source.
    7. Update the button state on confirmed delivery.
    """
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data is not None

    try:
        _, parts = decode_callback_payload(
            q.data, allowed_actions=("giffile",), max_parts=1
        )
        (token,) = parts
    except (CallbackDataError, ValueError):
        return

    cache_key, telegram_source = _gif_source_cache_key(q.message, token)
    debounce_key = f"giffile_lock:{cache_key}"
    if debounce_key in state.processing_gifs:
        try:
            await q.answer(Texts.GIF_FILE_QUEUE_TOAST, show_alert=False)
        except Exception:  # noqa: BLE001, S110 - optional progress feedback
            pass
        return
    # Claim the source before cache/UI/download awaits can interleave another export.
    state.processing_gifs.add(debounce_key)
    owned_paths: list[str] = []
    try:
        await _export_native_gif(
            q, context, token, cache_key, telegram_source, owned_paths
        )
    finally:
        state.processing_gifs.discard(debounce_key)
        # These are closed, request-owned files. Synchronous unlink cannot leave
        # a detached cleanup worker after repeated task cancellation.
        for owned_path in reversed(owned_paths):
            safe_remove(owned_path)


async def _export_native_gif(
    q: Any,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
    cache_key: str,
    telegram_source: Any,
    owned_paths: list[str],
) -> None:
    from telegram import InputFile

    from app.services.converter import MediaConverter

    delivery_key = _gif_document_delivery_key(q.message.chat_id, cache_key)
    # This callback owns one media send. Any prior success/uncertainty remains
    # authoritative, including old receipt keys after link-cache expiry.
    if not await current_job_can_retry():
        return

    # -- 1. Immediate UX feedback --
    try:
        await q.answer(Texts.GIF_FILE_PREPARING_TOAST, show_alert=False)
    # The preparation toast is optional; a failed adapter must not block GIF delivery.
    except Exception:  # noqa: BLE001, S110
        pass

    # Spinner: update button label to show work in progress
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        from app.bot.keyboards import build_sent_gif_keyboard

        spinner_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        Texts.BTN_SAVE_GIF_WAIT,
                        callback_data=encode_callback_data("giffile", token),
                    )
                ]
            ]
        )
        await q.message.edit_reply_markup(reply_markup=spinner_kb)
    # Spinner markup is optional and cannot determine the document delivery outcome.
    except Exception as e:  # noqa: BLE001
        logger.debug("Spinner button update failed: %s", e)

    # Only reuse an export of this exact sent source and request token.
    cached_doc_id = await state.gifdoc_cache.get(cache_key)
    if cached_doc_id and isinstance(cached_doc_id, str):
        # Storage errors propagate: they do not prove a cached file_id is bad.
        outcome, _ = await _send_gif_document_durable(
            context.bot,
            delivery_key,
            chat_id=q.message.chat_id,
            document=cached_doc_id,
            caption="🎞 Нативный .gif файл",
            reply_to_message_id=q.message.message_id,
            read_timeout=60,
            write_timeout=60,
        )
        if outcome is DeliveryOutcome.UNCERTAIN:
            return
        if outcome is DeliveryOutcome.FAILED:
            logger.warning("Cached gif file_id was rejected, regenerating")
        else:
            # Update the button after confirmed delivery.
            try:
                done_kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                Texts.BTN_SAVE_GIF_DONE,
                                callback_data=encode_callback_data("giffile", token),
                            )
                        ]
                    ]
                )
                await q.message.edit_reply_markup(reply_markup=done_kb)
            # Done-button UI is optional; the durable document receipt already records success.
            except Exception:  # noqa: BLE001, S110
                pass
            return

    # -- 3. Locate source video --
    # A token can be shared by several output messages. Use a token-only local
    # file only when the callback has no concrete Telegram source identity.
    video_path: str | None = (
        state.file_cache.get(token) if telegram_source is None else None
    )
    if video_path and not os.path.exists(video_path):
        video_path = None  # stale entry — evict implicitly

    if not video_path:
        # Recover exactly the media attached to this callback message.
        try:
            if telegram_source is not None:
                video_path = await _recover_telegram_gif_source(
                    context.bot, telegram_source, owned_paths
                )
        # Source recovery spans Telegram download and local I/O; a safe expiry UI follows failure.
        except Exception as e:  # noqa: BLE001
            logger.warning("on_save_as_gif_file: TG re-download failed: %s", e)

    if not video_path:
        try:
            await q.message.reply_text(Texts.GIF_FILE_EXPIRED, do_quote=True)
        # The expired-source notice is optional and may fail after the source is unavailable.
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            from app.bot.keyboards import build_sent_gif_keyboard

            await q.message.edit_reply_markup(
                reply_markup=build_sent_gif_keyboard(token)
            )
        # Restoring buttons on an expired source is optional; it must not mask expiry.
        except Exception:  # noqa: BLE001, S110
            pass
        return

    # -- Check size soft limit (50MB) to protect CPU and mobile clients --
    try:
        val_size = os.path.getsize(video_path)
        if val_size > 50 * 1024 * 1024:
            await q.answer(
                "⚠️ Исходник слишком большой для генерации GIF (>50MB).", show_alert=True
            )
            return
    except OSError:
        pass

    # -- Queue toast if semaphore is crowded --
    if state.gif_file_sem.locked():
        try:
            await q.answer(Texts.GIF_FILE_QUEUE_TOAST, show_alert=False)
        # Queue feedback is optional and must not abort a conversion waiting for capacity.
        except Exception:  # noqa: BLE001, S110
            pass

    # -- 5. Convert using palette-based native GIF export --
    gif_path: str | None
    if video_path.lower().endswith(".gif"):
        logger.info(
            "on_save_as_gif_file: bypassing FFmpeg, source is already .gif (%s)",
            video_path,
        )
        gif_path = video_path
    else:
        with process_owner_scope(token):
            gif_path = await MediaConverter.convert_to_native_gif(video_path)
        if gif_path and gif_path != video_path:
            owned_paths.append(gif_path)

    if not gif_path:
        try:
            await q.message.reply_text(Texts.GIF_FILE_ERROR, do_quote=True)
        # Conversion failure is established; sending its Telegram notice is best effort.
        except Exception:  # noqa: BLE001, S110
            pass
        return

    # -- 6. Send as document (reply to animation) --
    durable_send_active = False
    try:
        # Keep the upload handle owned by this task through send/cancellation;
        # offloading only open() could leave a late worker holding the source.
        with open(gif_path, "rb") as source:  # noqa: ASYNC230
            doc_input = InputFile(source, filename=f"animation_{token[:8]}.gif")
            durable_send_active = True
            outcome, sent = await _send_gif_document_durable(
                context.bot,
                delivery_key,
                chat_id=q.message.chat_id,
                document=doc_input,
                caption="🎞 Нативный .gif файл",
                reply_to_message_id=q.message.message_id,
                disable_content_type_detection=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
            durable_send_active = False
        if outcome is not DeliveryOutcome.SUCCESS:
            try:
                await q.message.reply_text(Texts.GIF_FILE_ERROR, do_quote=True)
            # The durable receipt determines delivery failure; its UI notice is best effort.
            except Exception:  # noqa: BLE001, S110
                pass
            return

        # Cache the exported file ID only for this exact sent source.
        if sent is not None and sent.document:
            await state.gifdoc_cache.set(cache_key, sent.document.file_id)
            logger.info(
                "on_save_as_gif_file: cached gif doc_file_id globally (%s)",
                cache_key,
            )

        # Update the button after confirmed delivery.
        try:
            done_kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            Texts.BTN_SAVE_GIF_DONE,
                            callback_data=encode_callback_data("giffile", token),
                        )
                    ]
                ]
            )
            await q.message.edit_reply_markup(reply_markup=done_kb)
        # Done-button UI is optional; the durable receipt and cached file_id are authoritative.
        except Exception:  # noqa: BLE001, S110
            pass

    except Exception:
        # Escapes from the durable helper include receipt-store failures;
        # the job runner must observe them instead of export-error recovery.
        if durable_send_active:
            raise
        logger.exception("on_save_as_gif_file: send_document failed")
        try:
            await q.message.reply_text(Texts.GIF_FILE_ERROR, do_quote=True)
        # Document/export failure is already logged; its Telegram notice is best effort.
        except Exception:  # noqa: BLE001, S110
            pass
