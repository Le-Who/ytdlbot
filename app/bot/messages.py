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
    is_tiktok_api_success = False
    is_slideshow = False
    from typing import Any

    tiktok_api_res: Any = None
    title = ""
    duration = "—"
    special_format = None
    formats = []
    thumbnail_url = None
    info_json_path = None
    api_source = None  # 'tikwm' or 'cobalt'

    if is_tiktok_url:
        from app.services.tikwm import TikWMService
        from app.core.config import ENABLE_COBALT_TIKTOK

        # Try TikWM first
        try:
            tikwm_res = await TikWMService.process(text)
            if tikwm_res.status in ("video", "picker"):
                is_tiktok_api_success = True
                tiktok_api_res = tikwm_res
                api_source = "tikwm"
                title = tikwm_res.title or "TikTok"
                is_slideshow = tikwm_res.is_slideshow
            else:
                logger.info(
                    "TikWM returned non-success status: %s (%s), falling back",
                    tikwm_res.status,
                    tikwm_res.error_message,
                )
        except Exception as e:
            logger.warning("TikWM failed, falling back: %s", e)

        # Try Cobalt if enabled and TikWM failed
        if not is_tiktok_api_success and ENABLE_COBALT_TIKTOK:
            from app.services.cobalt import CobaltService

            try:
                cobalt_res = await CobaltService.process(text)
                if cobalt_res.status in ("tunnel", "redirect", "picker"):
                    is_tiktok_api_success = True
                    tiktok_api_res = cobalt_res
                    api_source = "cobalt"
                    title = "TikTok"
                    is_slideshow = cobalt_res.is_slideshow
                else:
                    logger.info(
                        "Cobalt returned non-success status: %s (%s), falling back to yt-dlp",
                        cobalt_res.status,
                        cobalt_res.error_message,
                    )
            except Exception as e:
                logger.warning("Cobalt failed, falling back: %s", e)

    # If TikTok APIs fail, or it's not TikTok, process extraction fallbacks
    if not is_tiktok_api_success:
        if is_tiktok_url:
            # Bypass yt-dlp completely for TikTok to prevent proxy blocks.
            # We assume it's just a fallback video or slideshow for GalleryDL.
            from app.services.ytdlp.parsers import classify_tiktok_content
            from app.services.ytdlp.service import ExtractionResult, FormatItem

            _fallback_is_slideshow = classify_tiktok_content(text) == "slideshow"
            result = ExtractionResult(
                title="TikTok Content",
                formats=[
                    FormatItem(
                        format_id="gallerydl_fallback",
                        ext="mp4",
                        height=None,
                        filesize=None,
                        is_tiktok=True,
                        format_note="gallerydl_fallback",
                    )
                ],
                special_format="gallerydl_fallback"  # type: ignore
                if not _fallback_is_slideshow
                else None,
                duration_str="—",
                is_slideshow=_fallback_is_slideshow,
                info_json_path=None,
                thumbnail_url=None,
            )
            title = result.title
            formats = result.formats
            special_format = result.special_format
            duration = result.duration_str
            is_slideshow = result.is_slideshow
            info_json_path = result.info_json_path
            thumbnail_url = result.thumbnail_url

            # Cache the synthetic result briefly
            await state.info_cache.set(text, result, ttl=300)

        else:
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
        # Save specific api state for the slideshow
        if is_tiktok_api_success:
            from app.core.models import DownloadContext

            await state.link_cache.set(
                parse_token,
                DownloadContext(
                    page_url=text,
                    user_tag=user.username or "",  # using callback logic
                    chat_id=chat.id,
                    original_msg_id=status_msg.message_id,
                    api_source=api_source,
                    api_json=tiktok_api_res.__dict__,  # Will restore this in callbacks.py
                ),
            )
            # Override callback_data for API mode slideshows
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📸 Альбом", callback_data=f"apislide|{parse_token}|photo"
                        ),
                        InlineKeyboardButton(
                            "🎬 Видео", callback_data=f"apislide|{parse_token}|video"
                        ),
                    ]
                ]
            )
        else:
            reply_markup = build_slideshow_keyboard()

        try:
            await status_msg.edit_text(
                Texts.SLIDESHOW_DETECTED.format(title=html.escape(title)),
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception:
            await status_msg.delete()
            await status_msg.get_bot().send_message(
                chat_id=status_msg.chat_id,
                text=Texts.SLIDESHOW_DETECTED.format(title=html.escape(title)),
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
    elif is_tiktok_api_success and tiktok_api_res and tiktok_api_res.url:
        # Direct API video! Download and send immediately
        await status_msg.edit_text("⏳ Загрузка видео...")
        file_path = None
        if api_source == "tikwm":
            from app.services.tikwm import TikWMService

            file_path, err = await TikWMService.download_video(
                text, direct_video_url=tiktok_api_res.url
            )
        elif api_source == "cobalt":
            from app.services.cobalt import CobaltService

            file_path = await CobaltService.download_file(tiktok_api_res.url, "mp4")

        if not file_path:
            await status_msg.edit_text("⚠️ Ошибка загрузки видео.")
            return

        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{parse_token}")]]
        )
        state.file_cache[parse_token] = file_path  # for GIF conversions

        from app.services.sender import TelegramSender

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

        indexed_formats = formats[:8]
        fmt_index_map = {str(i): f.format_id for i, f in enumerate(indexed_formats)}
        if special_format:
            fmt_index_map[str(len(indexed_formats))] = special_format.format_id

        data.update(
            {
                "format_map": format_map,
                "size_map": size_map,
                "fmt_index_map": fmt_index_map,
            }
        )

        reply_markup = build_format_keyboard(formats, special_format)
        caption = f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}"

        if thumbnail_url:
            try:
                await status_msg.delete()
            except Exception:
                pass
            try:
                await msg.reply_photo(
                    photo=thumbnail_url,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to send thumbnail, falling back to text: %s", e)
                await msg.reply_text(
                    text=caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
        else:
            await status_msg.edit_text(
                caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
