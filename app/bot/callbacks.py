import os
import uuid
import asyncio
import time
import logging
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import NetworkError

from app.core import state
from app.core.process import run_subprocess
from app.core.config import BASE_URL, LINK_TTL_MINUTES, ENABLE_TELEGRAM_UPLOAD, TEMP_DIR
from app.core.utils import (
    check_rate_limit,
    safe_remove,
    render_progressbar,
    PROGRESS_RE,
    PROGRESS_DETAILS_RE,
    size_allowed,
)
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from app.bot.keyboards import build_format_keyboard

logger = logging.getLogger("app.bot.callbacks")

async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data
    page_url = data.get("page_url")
    if not page_url:
        await q.edit_message_text(
            "⚠️ Данные устарели. Пожалуйста, отправьте ссылку заново."
        )
        return

    cached = state.info_cache.get(page_url)
    if not cached:
        try:
            await q.edit_message_text("⏳ Кэш истек. Обновляю данные...")
            async with state.parsing_sem:
                title, formats, special_format, duration = await asyncio.to_thread(
                    state.ytdlp.list_formats, page_url
                )
            state.info_cache[page_url] = (title, formats, special_format, duration)
        except Exception as e:
            logger.error(f"[ON_BACK] Refresh error: {e}")
            await q.edit_message_text(
                "⚠️ Ошибка обновления данных. Отправьте ссылку заново."
            )
            return
    else:
        title, formats, special_format, duration = cached

    reply_markup = build_format_keyboard(formats, special_format)

    await q.edit_message_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )

async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Подготовка ссылки...")

    if not q.data:
        return

    try:
        _, format_id = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_pick: {e}")
        return

    data = context.user_data
    if not data.get("page_url"):
        await q.edit_message_text(
            "⚠️ Данные устарели. Пожалуйста, отправьте ссылку на видео еще раз."
        )
        return

    token = uuid.uuid4().hex
    state.link_cache[token] = {
        "page_url": data["page_url"],
        "format_id": format_id,
        "height": data["format_map"].get(format_id),
        "title": data["title"],
    }
    logger.info("token_created", extra={"token": token, "user_id": q.from_user.id, "chat_id": q.message.chat_id, "op": "token_create"})

    dl_link = f"{BASE_URL}/dl/{token}"

    kb = [[InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [
                InlineKeyboardButton(
                    "📤 Отправить файл в TG", callback_data=f"send|{token}"
                )
            ]
        )

    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])

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
        f"✅ <b>Готово{quality_str}</b>\n🔗 Ссылка ({LINK_TTL_MINUTES} мин):\n{html.escape(dl_link)}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
        parse_mode="HTML",
    )

async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚫 Отменяю...")

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
        state.cancel_cache[token] = True
        await q.edit_message_text("❌ Загрузка отменена пользователем.")
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_cancel: {e}")

async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚀 Загрузка началась")
    user_id = q.from_user.id

    if not check_rate_limit(user_id, chat_id=q.message.chat_id, limit=3):
        await q.edit_message_text("⚠️ Слишком часто скачиваете. Подождите.")
        return

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_send: {e}")
        return

    payload = state.link_cache.get(token)
    if not payload:
        await q.edit_message_text("⚠️ Ссылка устарела.")
        return

    dl_link = f"{BASE_URL}/dl/{token}"
    kb_error = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        ]
    )

    if state.tasks_sem.locked():
        await q.edit_message_text(
            "⚠️ Очередь переполнена. Скачайте по ссылке.", reply_markup=kb_error
        )
        return

    data = context.user_data
    fmt_size = data.get("size_map", {}).get(payload["format_id"])
    allowed, size_error = size_allowed(fmt_size)
    if not allowed:
        await q.edit_message_text(
            size_error + "\nПожалуйста, используйте прямую ссылку ниже.",
            reply_markup=kb_error,
        )
        return

    async def update_progress_ui(text, markup):
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"UI Update failed: {e}")

    async with state.tasks_sem:
        await q.edit_message_text("⏳ Начинаю загрузку...")
        
        from app.services.downloader import MediaSender # Lazy import to avoid circular dep if any

        file_path, error = await MediaSender.download_video(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            token,
            progress_callback=update_progress_ui
        )

        if error or not file_path:
            await q.edit_message_text(error or "⚠️ Ошибка.", reply_markup=kb_error)
            return

        await q.edit_message_text("📤 Отправляю в Telegram...")

        is_gif = payload["format_id"] == GIF_FORMAT_ID
        is_audio = payload["format_id"] == AUDIO_FORMAT_ID
        
        success = await MediaSender.send_file(
            context.bot,
            q.message.chat_id,
            file_path,
            is_audio=is_audio,
            is_gif=is_gif,
            caption="📹" if not is_audio else "🎵"
        )

        if success:
            await q.delete_message()
        else:
            await q.edit_message_text("⚠️ Ошибка при отправке файла.", reply_markup=kb_error)
        
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


async def on_convert_to_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'Send GIF' button press from Group Mode."""
    q = update.callback_query
    try:
        await q.answer("⏳ Конвертирую в GIF...")
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
        await q.answer("⚠️ Файл не найден или устарел.", show_alert=True)
        return

    # Check/Add to processing set (Debounce)
    if hasattr(state, "processing_gifs") and token in state.processing_gifs:
        await q.answer("⏳ У вас уже идет генерация...", show_alert=True)
        return
        
    if not hasattr(state, "processing_gifs"):
        state.processing_gifs = set()
        
    state.processing_gifs.add(token)
    
    try:
        # 2. Convert (Strip Audio)
        gif_path = await MediaSender.convert_to_gif_ffmpeg(video_path)
    finally:
        state.processing_gifs.discard(token)

    if not gif_path:
        # q.answer() was already called, so we can't show_alert=True via answer.
        try:
             await q.message.reply_text("⚠️ Ошибка конвертации.", quote=True)
        except:
             pass
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
        caption="🎬 GIF"
    )

    if success:
        pass
    else:
         try:
             await q.message.reply_text("⚠️ Не удалось отправить GIF.", quote=True)
         except:
             pass
    
    # Do NOT delete gif_path if it is the same as video_path (cached source)
    if gif_path != video_path:
        await asyncio.to_thread(safe_remove, gif_path)
