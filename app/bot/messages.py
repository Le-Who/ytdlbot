import asyncio
import logging
import html
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.utils import extract_supported_url
from app.bot.keyboards import build_format_keyboard
from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
)

logger = logging.getLogger("app.bot.messages")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    url = extract_supported_url(text)
    if not url:
        await update.message.reply_text(
            "❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest."
        )
        return

    text = url

    if not state.limiter.allow_user(user.id) or not state.limiter.allow_chat(
        update.effective_chat.id
    ):
        await update.message.reply_text("⚠️ Слишком часто. Подождите минуту.")
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    msg = await update.message.reply_text("🔎 Ищу видео...")

    cached = state.info_cache.get(text)
    if cached:
        logger.info(f"[CACHE] Hit: {text}")
        title, formats, special_format, duration = cached
    else:
        if text in state.inflight_parsing:
            logger.info(f"[PARSING] Waiting for inflight task: {text}")
            await state.inflight_parsing[text].wait()
            cached = state.info_cache.get(text)
            if cached:
                title, formats, special_format, duration = cached
            else:
                await msg.edit_text(
                    "❌ Ошибка при получении данных. Попробуйте еще раз."
                )
                return
        else:
            event = asyncio.Event()
            state.inflight_parsing[text] = event
            try:
                async with asyncio.timeout(300.0):
                    async with state.parsing_sem:
                        (
                            title,
                            formats,
                            special_format,
                            duration,
                        ) = await asyncio.to_thread(state.ytdlp.list_formats, text)
                state.info_cache[text] = (title, formats, special_format, duration)
            except asyncio.TimeoutError:
                await msg.edit_text("❌ Время ожидания истекло. Сервис недоступен.")
                return
            except AccessDeniedError:
                await msg.edit_text(
                    "❌ Доступ запрещен. Контент может быть приватным или требуется авторизация."
                )
                return
            except VideoNotFoundError:
                await msg.edit_text(
                    "❌ Видео не найдено. Проверьте правильность ссылки."
                )
                return
            except LiveStreamError:
                await msg.edit_text("❌ Прямые трансляции (Live) не поддерживаются.")
                return
            except ExtractionError as e:
                error_msg = str(e).lower()
                if "pinterest" in error_msg or "pin.it" in error_msg:
                    await msg.edit_text(
                        "❌ Ошибка загрузки с Pinterest. Попробуйте позже или используйте прямую ссылку на видео."
                    )
                else:
                    # e already contains the localized error message prefix
                    await msg.edit_text(f"❌ {e}")
                return
            except Exception as e:
                logger.error(f"Parse error: {e}", exc_info=True)
                await msg.edit_text(f"❌ Ошибка: {str(e)[:150]}")
                return
            finally:
                event.set()
                state.inflight_parsing.pop(text, None)

    format_map = {f.format_id: f.height for f in formats}
    format_map[special_format.format_id] = None

    size_map = {f.format_id: f.filesize for f in formats}
    size_map[special_format.format_id] = special_format.filesize

    context.user_data.update(
        {
            "page_url": text,
            "title": title,
            "format_map": format_map,
            "size_map": size_map,
        }
    )

    reply_markup = build_format_keyboard(formats, special_format)

    await msg.edit_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
