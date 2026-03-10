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
)

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

    if not await state.limiter.allow_user(
        user.id
    ) or not await state.limiter.allow_chat(chat.id):
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
    await state.cancel_cache.delete(parse_token)

    is_tiktok_url = "tiktok" in text.lower()
    is_cobalt_success = False
    cobalt_res = None

    if is_tiktok_url:
        from app.services.cobalt import CobaltService

        try:
            cobalt_res = await CobaltService.process(text)
            if cobalt_res.status in ("tunnel", "redirect", "picker"):
                is_cobalt_success = True
                title = "TikTok"
                is_slideshow = cobalt_res.is_slideshow
                info_json_path = None
                thumbnail_url = None
                formats = []
                special_format = None
                duration = "—"
        except Exception as e:
            logger.warning("Cobalt failed, falling back: %s", e)

    if not is_cobalt_success:
        cached = await state.info_cache.get(text)
        if cached:
            logger.info("Cache hit", extra={"url": text})
            result = cached
            title = result.title
            formats = result.formats
            special_format = result.special_format
            duration = result.duration_str
            is_slideshow = result.is_slideshow
            info_json_path = result.info_json_path
            thumbnail_url = result.thumbnail_url
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
                cached = await state.info_cache.get(text)
                if cached:
                    result = cached
                    title = result.title
                    formats = result.formats
                    special_format = result.special_format
                    duration = result.duration_str
                    is_slideshow = result.is_slideshow
                    info_json_path = result.info_json_path
                    thumbnail_url = result.thumbnail_url
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
                            result = await state.ytdlp.list_formats(text)
                            title = result.title
                            formats = result.formats
                            special_format = result.special_format
                            duration = result.duration_str
                            is_slideshow = result.is_slideshow
                            info_json_path = result.info_json_path
                            thumbnail_url = result.thumbnail_url
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
                    if await state.cancel_cache.get(parse_token):
                        await state.cancel_cache.delete(parse_token)
                        await status_msg.edit_text(Texts.CANCELLED)
                        return

                    await state.info_cache.set(text, result)
                except asyncio.TimeoutError:
                    await status_msg.edit_text(Texts.TIMEOUT_UNAVAILABLE)
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
        # Save specific cobalt state for the slideshow
        if is_cobalt_success:
            from app.core.models import DownloadContext

            await state.link_cache.set(
                parse_token,
                DownloadContext(
                    page_url=text,
                    user_tag=user.username or "",  # using callback logic
                    chat_id=chat.id,
                    original_msg_id=status_msg.message_id,
                    cobalt_json=cobalt_res.__dict__,  # Will restore this in callbacks.py
                ),
            )
            # Override callback_data for Cobalt mode slideshows
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📸 Альбом", callback_data=f"cbslide|{parse_token}|photo"
                        ),
                        InlineKeyboardButton(
                            "🎬 Видео", callback_data=f"cbslide|{parse_token}|video"
                        ),
                    ]
                ]
            )
        else:
            reply_markup = build_slideshow_keyboard()

        await status_msg.edit_text(
            Texts.SLIDESHOW_DETECTED.format(title=html.escape(title)),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    elif is_cobalt_success and cobalt_res and cobalt_res.url:
        # Direct Cobalt video! Download and send immediately
        await status_msg.edit_text("⏳ Загрузка видео...")
        from app.services.cobalt import CobaltService
        from app.services.sender import TelegramSender

        file_path = await CobaltService.download_file(cobalt_res.url, "mp4")
        if not file_path:
            await status_msg.edit_text("⚠️ Ошибка загрузки видео из Cobalt.")
            return

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{parse_token}")]]
        )
        state.file_cache[parse_token] = file_path  # for GIF conversions

        success = await TelegramSender.send_file(
            context.bot,
            chat.id,
            file_path,
            is_audio=False,
            is_gif=False,
            caption=f"👤 {user.mention_html()}",
            parse_mode="HTML",
            reply_markup=kb,
        )
        if success:
            try:
                await status_msg.delete()
                await msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text("⚠️ Ошибка отправки видео.")

    else:
        format_map = {f.format_id: f.height for f in formats}

        if special_format:
            format_map[special_format.format_id] = None

            size_map = {f.format_id: f.filesize for f in formats}
            size_map[special_format.format_id] = special_format.filesize
        else:
            size_map = {f.format_id: f.filesize for f in formats}

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
