import os
import uuid
import asyncio
import logging
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app.core import state
from app.core.config import (
    BASE_URL,
    LINK_TTL_MINUTES,
    ENABLE_TELEGRAM_UPLOAD,
    MAX_TG_UPLOAD_MB,
)
from app.core.utils import (
    safe_remove,
)
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from app.bot.keyboards import build_format_keyboard
from app.core.texts import Texts
from app.core.policy import size_allowed
from app.core.logging import set_correlation_id

__all__ = ["on_back", "on_pick", "on_cancel", "on_send", "on_convert_to_gif"]

logger = logging.getLogger("app.bot.callbacks")


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    data = context.user_data
    page_url = data.get("page_url")
    if not page_url:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    cached = state.info_cache.get(page_url)
    if not cached:
        try:
            await q.edit_message_text(Texts.CACHE_REFRESHING)
            async with state.parsing_sem:
                title, formats, special_format, duration = await asyncio.to_thread(
                    state.ytdlp.list_formats, page_url
                )
            state.info_cache[page_url] = (title, formats, special_format, duration)
        except Exception as e:
            logger.error(f"[ON_BACK] Refresh error: {e}")
            await q.edit_message_text(Texts.CACHE_REFRESH_FAIL)
            return
    else:
        title, formats, special_format, duration = cached

    reply_markup = build_format_keyboard(formats, special_format)

    await q.edit_message_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
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
        logger.error(f"Invalid callback data in on_pick: {e}")
        return

    data = context.user_data
    if not data.get("page_url"):
        await q.edit_message_text(Texts.DATA_EXPIRED_RESEND)
        return

    token = uuid.uuid4().hex
    state.link_cache[token] = {
        "page_url": data["page_url"],
        "format_id": format_id,
        "height": data["format_map"].get(format_id),
        "title": data["title"],
    }

    dl_link = f"{BASE_URL}/dl/{token}"

    kb = [[InlineKeyboardButton(Texts.BTN_DOWNLOAD_LINK, url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [
                InlineKeyboardButton(
                    Texts.BTN_SEND_TG, callback_data=f"send|{token}"
                )
            ]
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
        Texts.READY_LINK.format(quality=quality_str, ttl=LINK_TTL_MINUTES, link=html.escape(dl_link)),
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer(Texts.CANCELLING)

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
        state.cancel_cache[token] = True
        await q.edit_message_text(Texts.CANCELLED)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_cancel: {e}")


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
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
        logger.error(f"Invalid callback data in on_send: {e}")
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

    if state.tasks_sem.locked():
        await q.edit_message_text(Texts.QUEUE_FULL, reply_markup=kb_error)
        return

    data = context.user_data
    fmt_size = data.get("size_map", {}).get(payload["format_id"])
    if not size_allowed(fmt_size, target="telegram"):
        mb = fmt_size / (1024 * 1024)
        await q.edit_message_text(
            Texts.FILE_TOO_BIG.format(size_mb=mb, max_mb=MAX_TG_UPLOAD_MB),
            reply_markup=kb_error,
        )
        return

    async def update_progress_ui(text, markup):
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"UI Update failed: {e}")

    async with state.tasks_sem:
        await q.edit_message_text(Texts.STARTING_DOWNLOAD)

        from app.services.downloader import (
            MediaSender,
        )  # Lazy import to avoid circular dep if any

        file_path, error = await MediaSender.download_video(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            token,
            progress_callback=update_progress_ui,
        )

        if error or not file_path:
            await q.edit_message_text(error or Texts.GENERIC_ERROR_SHORT, reply_markup=kb_error)
            return

        await q.edit_message_text(Texts.SENDING_TO_TG)

        is_gif = payload["format_id"] == GIF_FORMAT_ID
        is_audio = payload["format_id"] == AUDIO_FORMAT_ID

        success = await MediaSender.send_file(
            context.bot,
            q.message.chat_id,
            file_path,
            is_audio=is_audio,
            is_gif=is_gif,
            caption="📹" if not is_audio else "🎵",
        )

        if success:
            await q.delete_message()
        else:
            await q.edit_message_text(Texts.SEND_ERROR, reply_markup=kb_error)

        # Cleanup is handled by MediaSender if it created a new file, but we should ensure cache policy
        # If it was a cached file, don't remove.
        # Actually MediaSender returns path. If it was from cache, existing logic holds.
        # If we want to remove after send to save space (unless reused for GIF), we might need logic.
        # For now, let TTLCache handle cleanup or periodic cleanup task (not in scope).
        # But wait, original code removed tmp_path immediately.
        # If we rely on cache, we must not remove it yet.
        # We can implement a cleanup job or rely on OS temp cleaner, but for now we follow the "Reuse" requirement.
        # To avoid disk fill up, we could remove if it's NOT in file_cache, but MediaSender puts it there.
        # We'll leave it in cache.


async def on_convert_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Send GIF' button press from Group Mode."""
    q = update.callback_query
    try:
        await q.answer(Texts.GIF_CONVERTING)
        await q.edit_message_reply_markup(None)
    except Exception as e:
        logger.warning(f"Callback answer failed (query too old?): {e}")

    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        return

    from app.services.downloader import MediaSender

    # 1. Get file path from cache
    video_path = state.file_cache.get(token)
    if not video_path or not os.path.exists(video_path):
        try:
            await q.message.reply_text("⚠️ Файл не найден или устарел.", quote=True)
        except Exception as e:
            logger.warning(f"Failed to reply about missing file: {e}")
        return

    # Check/Add to processing set (Debounce) — atomic under lock
    async with state.conversion_lock:
        if token in state.processing_gifs:
            try:
                await q.message.reply_text("⏳ У вас уже идет генерация...", quote=True)
            except Exception as e:
                logger.warning(f"Failed to reply about in-progress GIF: {e}")
            return
        state.processing_gifs.add(token)

    try:
        # 2. Convert (Strip Audio)
        gif_path = await MediaSender.convert_to_gif_ffmpeg(video_path)
    finally:
        state.processing_gifs.discard(token)

    if not gif_path:
        try:
            await q.message.reply_text("⚠️ Ошибка конвертации.", quote=True)
        except Exception as e:
            logger.warning(f"Failed to reply about conversion error: {e}")
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
            await q.message.reply_text("⚠️ Не удалось отправить GIF.", quote=True)
        except Exception as e:
            logger.warning(f"Failed to reply about send error: {e}")

    # Do NOT delete gif_path if it is the same as video_path (cached source)
    if gif_path != video_path:
        await asyncio.to_thread(safe_remove, gif_path)
