import asyncio
import logging
import html
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.utils import extract_supported_url
from app.bot.keyboards import build_format_keyboard, build_slideshow_keyboard
from app.core.texts import Texts
from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
    DirectDownloadReady,
)
from app.services.sender import TelegramSender

logger = logging.getLogger("app.bot.messages")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    chat = update.effective_chat
    assert user is not None and msg is not None and chat is not None
    text = (msg.text or "").strip()

    url = extract_supported_url(text)
    if not url:
        await msg.reply_text(Texts.URL_NOT_SUPPORTED)
        return

    text = url

    if not state.limiter.allow_user(user.id) or not state.limiter.allow_chat(chat.id):
        await msg.reply_text(Texts.RATE_LIMITED)
        return

    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)

    parse_token = uuid.uuid4().hex[:8]
    kb_cancel = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Отмена", callback_data=f"cancel_parse|{parse_token}"
                )
            ]
        ]
    )
    status_msg = await msg.reply_text(Texts.SEARCHING, reply_markup=kb_cancel)
    state.cancel_cache.pop(parse_token, None)

    cached = state.info_cache.get(text)
    if cached:
        logger.info("Cache hit", extra={"url": text})
        (
            title,
            formats,
            special_format,
            duration,
            is_slideshow,
            info_json_path,
            thumbnail_url,
        ) = cached
    else:
        if text in state.inflight_parsing:
            logger.info("Waiting for inflight parse", extra={"url": text})
            try:
                await asyncio.wait_for(
                    state.inflight_parsing[text].wait(), timeout=300.0
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(Texts.TIMEOUT_RETRY)
                return
            cached = state.info_cache.get(text)
            if cached:
                (
                    title,
                    formats,
                    special_format,
                    duration,
                    is_slideshow,
                    info_json_path,
                    thumbnail_url,
                ) = cached
            else:
                await status_msg.edit_text(Texts.FETCH_ERROR_RETRY)
                return
        else:
            event = asyncio.Event()
            state.inflight_parsing[text] = event
            try:
                async with asyncio.timeout(300.0):
                    async with state.parsing_sem:
                        import time as _time
                        from app.core.metrics import metrics as _m

                        _ext_start = _time.monotonic()
                        (
                            title,
                            formats,
                            special_format,
                            duration,
                            is_slideshow,
                            info_json_path,
                            thumbnail_url,
                        ) = await asyncio.get_event_loop().run_in_executor(
                            state.ytdlp_executor, state.ytdlp.list_formats, text
                        )
                        _platform = (
                            "youtube"
                            if "youtu" in text
                            else "tiktok"
                            if "tiktok" in text
                            else "other"
                        )
                        _m.extraction_duration.observe(
                            _time.monotonic() - _ext_start,
                            platform=_platform,
                        )

                # Check if user cancelled while parsing
                if state.cancel_cache.get(parse_token):
                    state.cancel_cache.pop(parse_token, None)
                    await status_msg.edit_text(Texts.CANCELLED)
                    return

                state.info_cache[text] = (
                    title,
                    formats,
                    special_format,
                    duration,
                    is_slideshow,
                    info_json_path,
                    thumbnail_url,
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(Texts.TIMEOUT_UNAVAILABLE)
                return
            except DirectDownloadReady as dd:
                await status_msg.edit_text(
                    "📦 Загрузка через альтернативный источник..."
                )
                sent = await TelegramSender.send_file(
                    bot=context.bot,
                    chat_id=chat.id,
                    file_path=dd.video_path,
                    caption=f"🎬 {html.escape(dd.title)}",
                    parse_mode="HTML",
                    reply_to_message_id=msg.message_id,
                )
                if sent:
                    await status_msg.delete()
                else:
                    await status_msg.edit_text(
                        "❌ Не удалось отправить видео (файл слишком большой?)"
                    )
                return
            except AccessDeniedError:
                await status_msg.edit_text(Texts.ACCESS_DENIED)
                return
            except VideoNotFoundError:
                await status_msg.edit_text(Texts.VIDEO_NOT_FOUND)
                return
            except LiveStreamError:
                await status_msg.edit_text(Texts.LIVE_NOT_SUPPORTED)
                return
            except ExtractionError as e:
                error_msg = str(e).lower()
                if "pinterest" in error_msg or "pin.it" in error_msg:
                    await status_msg.edit_text(Texts.PINTEREST_ERROR)
                else:
                    await status_msg.edit_text(f"❌ {e}")
                return
            except Exception as e:
                logger.error("Parse error", extra={"error": str(e)}, exc_info=True)
                await status_msg.edit_text(
                    Texts.GENERIC_ERROR.format(detail=str(e)[:150])
                )
                return
            finally:
                event.set()
                state.inflight_parsing.pop(text, None)

    # Store common data
    data = context.user_data
    assert data is not None
    data["page_url"] = text
    data["title"] = title
    data["is_slideshow"] = is_slideshow
    data["info_json_path"] = info_json_path if not is_slideshow else None
    data["thumbnail_url"] = thumbnail_url

    if is_slideshow:
        # TikTok slideshow — show photo/video choice keyboard
        reply_markup = build_slideshow_keyboard()

        await status_msg.edit_text(
            Texts.SLIDESHOW_DETECTED.format(title=html.escape(title)),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        # Normal video — show format selection keyboard
        format_map = {f.format_id: f.height for f in formats}
        format_map[special_format.format_id] = None

        size_map = {f.format_id: f.filesize for f in formats}
        size_map[special_format.format_id] = special_format.filesize

        data.update(
            {
                "format_map": format_map,
                "size_map": size_map,
            }
        )

        reply_markup = build_format_keyboard(formats, special_format)
        caption = f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}"

        if thumbnail_url:
            try:
                await status_msg.delete()
            except Exception:
                pass
            await msg.reply_photo(
                photo=thumbnail_url,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
