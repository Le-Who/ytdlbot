import os
import uuid
import asyncio
import logging
import html
from telegram import (
    Update,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    LinkPreviewOptions,
)
from telegram.ext import ContextTypes

from app.core import state
from app.core.config import (
    BASE_URL,
    LINK_TTL_MINUTES,
    ENABLE_TELEGRAM_UPLOAD,
)
from app.core.utils import (
    safe_remove,
)
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID, SLIDESHOW_PHOTO_FORMAT_ID
from app.bot.keyboards import build_format_keyboard
from app.core.texts import Texts
from app.core.logging import set_correlation_id
from app.services.downloader import MediaSender, MAX_TELEGRAM_ALBUM_SIZE
from app.core.models import DownloadContext
from app.services.orchestrator import DownloadOrchestrator

__all__ = [
    "on_back",
    "on_pick",
    "on_cancel",
    "on_send",
    "on_convert_to_gif",
    "on_slideshow",
]

logger = logging.getLogger("app.bot.callbacks")


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

    cached = state.info_cache.get(page_url)
    if not cached:
        try:
            await q.edit_message_text(Texts.CACHE_REFRESHING)
            async with state.parsing_sem:
                result = await asyncio.to_thread(state.ytdlp.list_formats, page_url)
            state.info_cache[page_url] = result

            title = result.title
            formats = result.formats
            special_format = result.special_format
            duration = result.duration_str
            is_slideshow = result.is_slideshow
            thumbnail_url = result.thumbnail_url
        except Exception as e:
            logger.error("Refresh error on back", extra={"error": str(e)})
            await q.edit_message_text(Texts.CACHE_REFRESH_FAIL)
            return
    else:
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
            except Exception:
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
    except Exception:
        pass

    if not q.data:
        return

    try:
        _, format_id = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_pick", extra={"error": str(e)})
        return

    data = context.user_data
    assert data is not None
    if not data.get("page_url"):
        await q.edit_message_text(Texts.DATA_EXPIRED_RESEND)
        return

    token = uuid.uuid4().hex
    state.link_cache[token] = DownloadContext(
        page_url=data["page_url"],
        format_id=format_id,
        height=data["format_map"].get(format_id),
        title=data["title"],
        info_json_path=data.get("info_json_path"),
    )

    dl_link = f"{BASE_URL}/dl/{token}"

    kb = [[InlineKeyboardButton(Texts.BTN_DOWNLOAD_LINK, url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [InlineKeyboardButton(Texts.BTN_SEND_TG, callback_data=f"send|{token}")]
        )

    kb.append([InlineKeyboardButton(Texts.BTN_BACK, callback_data="back")])

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

    await q.edit_message_text(
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
        _, token = q.data.split("|", 1)
        state.cancel_cache[token] = True
        await q.edit_message_text(Texts.CANCELLED)
    except (ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_cancel", extra={"error": str(e)})


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    assert q is not None and q.from_user is not None and isinstance(q.message, Message)
    await q.answer(Texts.DOWNLOAD_STARTED)
    user_id = q.from_user.id

    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass

    if not state.limiter.allow_user(user_id) or not state.limiter.allow_chat(
        q.message.chat_id
    ):
        await q.edit_message_text(Texts.TOO_MANY_REQUESTS)
        return

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_send", extra={"error": str(e)})
        return

    set_correlation_id(token)
    payload = state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.LINK_EXPIRED)
        return

    dl_link = f"{BASE_URL}/dl/{token}"
    kb_error = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(Texts.BTN_DOWNLOAD_LINK, url=dl_link)],
            [InlineKeyboardButton(Texts.BTN_BACK, callback_data="back")],
        ]
    )

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    data = context.user_data
    assert data is not None
    fmt_size = data.get("size_map", {}).get(payload.format_id)

    from typing import Optional
    async def update_progress_ui(text: str, markup: Optional[object] = None) -> None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except Exception as e:
            logger.warning("UI update failed", extra={"error": str(e)})

    success = await DownloadOrchestrator.process_download(
        token=token,
        chat_id=q.message.chat_id,
        bot=context.bot,
        payload=payload,
        fmt_size=fmt_size,
        update_ui=update_progress_ui,
        kb_error=kb_error,
    )

    if success:
        try:
            await q.delete_message()
        except Exception:
            pass


async def on_convert_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Send GIF' button press from Group Mode."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data is not None
    try:
        await q.answer(Texts.GIF_CONVERTING)
        await q.edit_message_reply_markup(None)
    except Exception as e:
        logger.warning("Callback answer failed", extra={"error": str(e)})

    _, token = q.data.split("|", 1)

    # 1. Get file path from cache
    video_path = state.file_cache.get(token)
    if not video_path or not os.path.exists(video_path):
        try:
            await q.message.reply_text(Texts.GIF_FILE_EXPIRED, do_quote=True)
        except Exception as e:
            logger.warning(
                "Failed to reply about missing file", extra={"error": str(e)}
            )
        return

    # Check/Add to processing set (Debounce) — atomic under lock
    async with state.conversion_sem:
        if token in state.processing_gifs:
            try:
                await q.message.reply_text(Texts.GIF_ALREADY_IN_PROGRESS, do_quote=True)
            except Exception as e:
                logger.warning(
                    "Failed to reply about in-progress GIF", extra={"error": str(e)}
                )
            return
        state.processing_gifs.add(token)

    try:
        # 2. Convert (Strip Audio)
        gif_path = await MediaSender.convert_to_gif_ffmpeg(video_path)
    finally:
        state.processing_gifs.discard(token)

    if not gif_path:
        try:
            await q.message.reply_text(Texts.GIF_CONVERSION_ERROR, do_quote=True)
        except Exception as e:
            logger.warning(
                "Failed to reply about conversion error", extra={"error": str(e)}
            )
        return

    # 3. Send as Reply to the VIDEO message
    target_msg_id = q.message.message_id

    # Sending GIF
    success = await MediaSender.send_file(
        context.bot,
        q.message.chat_id,
        gif_path,
        is_gif=True,
        reply_to_message_id=target_msg_id,
        caption="🎬 GIF",
    )

    if not success:
        try:
            await q.message.reply_text(Texts.GIF_SEND_ERROR, do_quote=True)
        except Exception as e:
            logger.warning("Failed to reply about send error", extra={"error": str(e)})

    # Do NOT delete gif_path if it is the same as video_path (cached source)
    if gif_path != video_path:
        await asyncio.to_thread(safe_remove, gif_path)


async def on_slideshow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles slideshow button presses (photos or video)."""
    q = update.callback_query
    assert q is not None and q.from_user is not None and isinstance(q.message, Message)
    await q.answer(Texts.SLIDESHOW_DOWNLOADING)
    user_id = q.from_user.id

    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass

    if not state.limiter.allow_user(user_id) or not state.limiter.allow_chat(
        q.message.chat_id
    ):
        await q.edit_message_text(Texts.TOO_MANY_REQUESTS)
        return

    if not q.data:
        return

    try:
        _, mode = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_slideshow", extra={"error": str(e)})
        return

    data = context.user_data
    assert data is not None
    page_url = data.get("page_url")
    if not page_url:
        await q.edit_message_text(Texts.DATA_EXPIRED_RESEND)
        return

    is_photo_mode = mode == SLIDESHOW_PHOTO_FORMAT_ID

    if state.tasks_sem.locked():
        await q.edit_message_text(Texts.QUEUE_FULL)
        return

    await state.tasks_sem.acquire()
    try:
        await q.edit_message_text(Texts.SLIDESHOW_DOWNLOADING)

        result, error = await MediaSender.download_slideshow(page_url)

        if error or not result:
            await q.edit_message_text(error or Texts.SLIDESHOW_ERROR)
            return

        try:
            if is_photo_mode:
                # Send as photo album
                await q.edit_message_text(Texts.SLIDESHOW_SENDING)

                total = len(result.images)
                caption = "📸"
                if total > MAX_TELEGRAM_ALBUM_SIZE:
                    caption += f"\n{Texts.SLIDESHOW_TRUNCATED.format(total=total)}"

                success = await MediaSender.send_slideshow_photos(
                    context.bot,
                    q.message.chat_id,
                    result.images,
                    caption=caption,
                )

                if success:
                    await q.delete_message()
                else:
                    await q.edit_message_text(Texts.SEND_ERROR)
            else:
                # Convert to video and send
                await q.edit_message_text(Texts.SLIDESHOW_CONVERTING)

                video_path = await MediaSender.images_to_video(
                    result.images, result.audio
                )

                if not video_path:
                    await q.edit_message_text(Texts.SLIDESHOW_ERROR)
                    return

                await q.edit_message_text(Texts.SENDING_TO_TG)

                success = await MediaSender.send_file(
                    context.bot,
                    q.message.chat_id,
                    video_path,
                    caption="🎬",
                )

                if success:
                    await q.delete_message()
                else:
                    await q.edit_message_text(Texts.SEND_ERROR)

                # Cleanup video file
                await asyncio.to_thread(safe_remove, video_path)
        finally:
            # Always cleanup slideshow download directory
            await asyncio.to_thread(MediaSender.cleanup_slideshow, result)
    finally:
        state.tasks_sem.release()
