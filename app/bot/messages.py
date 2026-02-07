import asyncio
import logging
import html
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.utils import URL_RE, check_rate_limit, is_supported_url
from app.bot.keyboards import build_format_keyboard

logger = logging.getLogger("app.bot.messages")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest."
        )
        return

    text = match.group(0).rstrip(".,!:;)")

    if not is_supported_url(text):
        await update.message.reply_text(
            "❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest."
        )
        return

    if not check_rate_limit(user.id, limit=10):
        await update.message.reply_text("⚠️ Слишком часто. Подождите минуту.")
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    msg = await update.message.reply_text("🔎 Ищу видео...")

    cached = state.info_cache.get(text)
    if cached:
        logger.info(f"[CACHE] Hit: {text}")
        title, formats, audio, duration = cached
    else:
        if text in state.inflight_parsing:
            logger.info(f"[PARSING] Waiting for inflight task: {text}")
            await state.inflight_parsing[text].wait()
            cached = state.info_cache.get(text)
            if cached:
                title, formats, audio, duration = cached
            else:
                await msg.edit_text(
                    "❌ Ошибка при получении данных. Попробуйте еще раз."
                )
                return
        else:
            event = asyncio.Event()
            state.inflight_parsing[text] = event
            try:
                async with state.parsing_sem:
                    title, formats, audio, duration = await asyncio.to_thread(
                        state.ytdlp.list_formats, text
                    )
                state.info_cache[text] = (title, formats, audio, duration)
            except Exception as e:
                logger.error(f"Parse error: {e}", exc_info=True)
                error_msg = str(e)
                if "403" in error_msg or "forbidden" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Доступ запрещен. Контент может быть приватным или требуется авторизация."
                    )
                elif "404" in error_msg or "not found" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Видео не найдено. Проверьте правильность ссылки."
                    )
                elif "pinterest" in error_msg.lower() or "pin.it" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Ошибка загрузки с Pinterest. Попробуйте позже или используйте прямую ссылку на видео."
                    )
                else:
                    await msg.edit_text(f"❌ Ошибка: {error_msg[:150]}")
                return
            finally:
                event.set()
                state.inflight_parsing.pop(text, None)

    format_map = {f.format_id: f.height for f in formats}
    format_map[audio.format_id] = None

    size_map = {f.format_id: f.filesize for f in formats}
    size_map[audio.format_id] = audio.filesize

    context.user_data.update(
        {
            "page_url": text,
            "title": title,
            "format_map": format_map,
            "size_map": size_map,
        }
    )

    reply_markup = build_format_keyboard(formats, audio)

    await msg.edit_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
