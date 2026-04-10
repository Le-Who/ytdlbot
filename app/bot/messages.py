import asyncio
import logging
import html
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import extract_url_from_update
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

    url, section = extract_url_from_update(msg)
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

    # ── Instagram Early Intercept ────────────────────────────────────────
    from app.services.instagram import is_instagram_url

    if is_instagram_url(text):
        await _handle_instagram(update, context, text, parse_token, section)
        return
    # ── End Instagram Intercept ──────────────────────────────────────────

    # ── Twitter / X Early Intercept ──────────────────────────────────────
    is_twitter_url = "x.com" in text.lower() or "twitter.com" in text.lower()
    if is_twitter_url:
        handled = await _handle_twitter(update, context, text, parse_token, section)
        if handled:
            return
    # ── End Twitter / X Intercept ────────────────────────────────────────

    # ── User Preferences Fast-Path ───────────────────────────────────────
    # If the user has set a default format/quality, skip the picker entirely.
    # Instagram and Twitter are handled by their own intercepts above.
    from app.core.user_prefs import get_prefs

    _prefs = await get_prefs(user.id)
    _default_fmt = _prefs.get("default_format")
    _default_quality = _prefs.get("default_quality")  # int or None

    if _default_fmt == "audio":
        from app.bot.commands import cmd_mp3

        await cmd_mp3(update, context)
        return

    if _default_fmt == "video" or _default_quality is not None:
        # Build a height-constrained format string when a quality is set
        if _default_quality:
            _sz_pref = f"{MAX_TG_UPLOAD_MB}M"
            _fmt_pref = (
                f"bestvideo[height<={_default_quality}][ext=mp4]+bestaudio[ext=m4a]"
                f"/best[height<={_default_quality}][ext=mp4]"
                f"/bestvideo[height<={_default_quality}]+bestaudio"
                f"/best[height<={_default_quality}][filesize<{_sz_pref}]/best"
            )
        else:
            from app.bot.commands import _MP4_FORMAT as _fmt_pref  # type: ignore[attr-defined]
        from app.bot.commands import _fast_download

        await _fast_download(
            update, context, format_id=_fmt_pref, is_audio=False
        )
        return
    # ── End User Preferences Fast-Path ───────────────────────────────────

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
                special_format=(
                    "gallerydl_fallback"  # type: ignore
                    if not _fallback_is_slideshow
                    else None
                ),
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
            if isinstance(cached, dict):
                from app.services.ytdlp.models import ExtractionResult

                result = ExtractionResult.from_dict(cached)
            else:
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
                    if isinstance(cached, dict):
                        from app.services.ytdlp.models import ExtractionResult

                        result = ExtractionResult.from_dict(cached)
                    else:
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
    data["section"] = section

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
                    section=section,
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
            from app.services.orchestrator import _cobalt_url_head_size

            # OPT-4: Attempt direct URL delivery if file is small enough for TG server-fetch
            _cobalt_size = await _cobalt_url_head_size(tiktok_api_res.url)
            _tg_url_limit = int(19.5 * 1024 * 1024)
            if _cobalt_size is not None and _cobalt_size <= _tg_url_limit:
                logger.info(
                    "OPT-4 TikTok Cobalt URL delivery: %.1f MB (url mode)",
                    _cobalt_size / 1e6,
                )
                file_path = tiktok_api_res.url  # Pass URL directly to send_file
            else:
                file_path = await CobaltService.download_file(tiktok_api_res.url, "mp4")

        if not file_path:
            await status_msg.edit_text("⚠️ Ошибка загрузки видео.")
            return

        from app.bot.keyboards import build_video_keyboard

        kb = build_video_keyboard(parse_token)
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
                "youtube_fallback": result.youtube_fallback,
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


# ── Instagram Handler ────────────────────────────────────────────────────────


async def _handle_instagram(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    parse_token: str,
    section: str | None,
) -> None:
    """Handle Instagram URLs with rich selection UX."""
    from app.services.instagram import (
        InstagramService,
        parse_instagram_url,
    )

    msg = update.message
    user = update.effective_user
    chat = update.effective_chat
    assert msg is not None and user is not None and chat is not None

    url_type, target, item_id = parse_instagram_url(url)

    # ── Direct post/reel: download immediately via Cobalt ────────
    if url_type == "post" and target:
        status_msg = await msg.reply_text("⏳ Скачиваю пост из Instagram...")
        file_path, error = await InstagramService.download_post(url)
        if error or not file_path:
            await status_msg.edit_text(error or Texts.IG_DOWNLOAD_ERROR)
            return

        from app.services.sender import TelegramSender

        success = await TelegramSender.send_file(
            context.bot,
            chat.id,
            file_path,
            is_audio=False,
            is_gif=False,
            caption=f"📷 Instagram • {user.mention_html()}",
            parse_mode="HTML",
        )
        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text(Texts.IG_DOWNLOAD_ERROR)
        return

    # ── Direct story link with specific item_id ──────────────────
    if url_type == "stories" and target and item_id:
        status_msg = await msg.reply_text(Texts.IG_DOWNLOADING.format(type="историю"))

        profile_media = await InstagramService.get_profile_media(target)
        if profile_media.error:
            await status_msg.edit_text(profile_media.error)
            return

        story = next((s for s in profile_media.stories if s.mediaid == item_id), None)
        if not story:
            await status_msg.edit_text("⚠️ История не найдена или уже истекла.")
            return

        file_path, error = await InstagramService.download_story_item(story)
        if error or not file_path:
            await status_msg.edit_text(error or Texts.IG_DOWNLOAD_ERROR)
            return

        from app.services.sender import TelegramSender

        success = await TelegramSender.send_file(
            context.bot,
            chat.id,
            file_path,
            is_audio=False,
            is_gif=False,
            caption=f"📷 @{target} • {story.label}",
        )
        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text(Texts.IG_DOWNLOAD_ERROR)
        return

    # ── Direct highlight link ─────────────────────────────────────
    if url_type == "highlight" and target:
        status_msg = await msg.reply_text("⏳ Загружаю хайлайт…")
        items, error = await InstagramService.get_highlight_items(target)
        if error or not items:
            await status_msg.edit_text(error or "⚠️ Хайлайт пуст или не найден.")
            return

        hl_cache_key = f"{parse_token}_hl_{target}"
        hl_items_data = [
            {
                "mediaid": item.mediaid,
                "is_video": item.is_video,
                "url": item.url,
                "thumbnail_url": item.thumbnail_url,
                "timestamp": item.timestamp.isoformat(),
                "duration": item.duration,
                "typename": item.typename,
            }
            for item in items
        ]
        await state.link_cache.set(hl_cache_key, hl_items_data)

        lines = [f"📁 <b>{target}</b>\n"]
        for i, item in enumerate(items, 1):
            emoji = "🎬" if item.is_video else "📸"
            ts = item.timestamp.strftime("%d.%m %H:%M")
            dur = f" ({int(item.duration)}с)" if item.duration else ""
            lines.append(f"{i}. {emoji} {ts}{dur}")

        text_content = "\n".join(lines)

        btn_rows = []
        row_buf = []
        for i, item in enumerate(items):
            row_buf.append(
                InlineKeyboardButton(
                    str(i + 1),
                    callback_data=f"ig_hl_dl|{hl_cache_key}|{item.mediaid}",
                )
            )
            if len(row_buf) >= 5:
                btn_rows.append(row_buf)
                row_buf = []
        if row_buf:
            btn_rows.append(row_buf)

        btn_rows.append(
            [
                InlineKeyboardButton(
                    "📥 Скачать все", callback_data=f"ig_hl_dl_all|{hl_cache_key}"
                )
            ]
        )

        await status_msg.edit_text(
            text_content, reply_markup=InlineKeyboardMarkup(btn_rows), parse_mode="HTML"
        )
        return

    # ── Profile or stories link (no specific item) → Rich Selection UI ──
    if url_type in ("profile", "stories") and target:
        status_msg = await msg.reply_text(
            Texts.IG_LOADING_PROFILE.format(username=target),
            parse_mode="HTML",
        )

        profile_media = await InstagramService.get_profile_media(target)
        if profile_media.error:
            await status_msg.edit_text(profile_media.error)
            return

        if not profile_media.stories and not profile_media.highlights:
            await status_msg.edit_text(Texts.IG_NO_CONTENT.format(username=target))
            return

        # Cache stories and highlights for callback retrieval
        from app.core.models import DownloadContext

        ig_cache_data = {
            "stories": [
                {
                    "mediaid": s.mediaid,
                    "is_video": s.is_video,
                    "url": s.url,
                    "thumbnail_url": s.thumbnail_url,
                    "timestamp": s.timestamp.isoformat(),
                    "duration": s.duration,
                    "typename": s.typename,
                }
                for s in profile_media.stories
            ],
            "highlights": [
                {
                    "highlight_id": h.highlight_id,
                    "title": h.title,
                    "cover_url": h.cover_url,
                    "item_count": h.item_count,
                }
                for h in profile_media.highlights
            ],
        }

        await state.link_cache.set(
            parse_token,
            DownloadContext(
                page_url=url,
                user_tag=user.username or "",
                chat_id=chat.id,
                original_msg_id=status_msg.message_id,
                api_source="instagram",
                api_json=ig_cache_data,
                section=section,
            ),
        )

        # Build the menu keyboard
        buttons = []
        if profile_media.stories:
            buttons.append(
                InlineKeyboardButton(
                    f"📸 Истории ({len(profile_media.stories)})",
                    callback_data=f"ig_stories|{parse_token}",
                )
            )
        if profile_media.highlights:
            buttons.append(
                InlineKeyboardButton(
                    f"📁 Хайлайты ({len(profile_media.highlights)})",
                    callback_data=f"ig_highlights|{parse_token}",
                )
            )

        rows = []
        for i in range(0, len(buttons), 2):
            rows.append(buttons[i : i + 2])

        if profile_media.stories:
            rows.append(
                [
                    InlineKeyboardButton(
                        "📥 Скачать все истории",
                        callback_data=f"ig_dl_all|{parse_token}",
                    )
                ]
            )

        reply_markup = InlineKeyboardMarkup(rows)
        await status_msg.edit_text(
            Texts.IG_MENU.format(username=target),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return

    # ── Unknown Instagram URL format ─────────────────────────────
    await msg.reply_text(
        "⚠️ Не удалось распознать ссылку Instagram. "
        "Поддерживаются: профили, истории, хайлайты и посты/рилсы."
    )


# ── Twitter / X Handler ──────────────────────────────────────────────────────


async def _handle_twitter(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    parse_token: str,
    section: str | None,
) -> bool:
    """Handle Twitter/X URLs using Cobalt. Returns True if handled, False if fallback needed."""
    msg = update.message
    user = update.effective_user
    chat = update.effective_chat
    assert msg is not None and user is not None and chat is not None

    from app.services.cobalt import CobaltService
    from app.services.sender import TelegramSender
    import os

    status_msg = await msg.reply_text("⏳ Поиск в X (Twitter)...")

    try:
        res = await CobaltService.process(url)

        if res.status == "error":
            logger.warning(
                "Cobalt failed for Twitter: %s, falling back to yt-dlp",
                res.error_message,
            )
            await status_msg.delete()
            return False

        if res.status in ("tunnel", "redirect"):
            if not res.url:
                await status_msg.delete()
                return False

            await status_msg.edit_text("⏳ Загрузка видео с X...")
            # OPT-4: Direct URL delivery for small files (< 19.5 MB)
            from app.services.orchestrator import _cobalt_url_head_size
            _x_size = await _cobalt_url_head_size(res.url)
            _tg_url_limit = int(19.5 * 1024 * 1024)
            if _x_size is not None and _x_size <= _tg_url_limit:
                logger.info("OPT-4 X/Twitter URL delivery: %.1f MB", _x_size / 1e6)
                file_path = res.url  # URL delivered to TG server directly
            else:
                _downloaded = await CobaltService.download_file(res.url, "mp4")
                if not _downloaded:
                    await status_msg.edit_text("⚠️ Ошибка загрузки видео с X.")
                    return True
                file_path = _downloaded
            if not file_path:
                await status_msg.edit_text("⚠️ Ошибка загрузки видео с X.")
                return True

            await status_msg.edit_text("⏳ Отправка видео с X...")
            success = await TelegramSender.send_file(
                context.bot,
                chat.id,
                file_path,
                is_audio=False,
                is_gif=False,
                caption=f"🐦 X (Twitter) • {user.mention_html()}",
                parse_mode="HTML",
            )

            if success:
                try:
                    await status_msg.delete()
                    await msg.delete()
                except Exception:
                    pass
            else:
                await status_msg.edit_text("⚠️ Ошибка отправки видео с X.")

            # Clean up
            try:
                os.unlink(file_path)
            except Exception:
                pass
            return True

        if res.status == "picker":
            # Post with multiple media
            await status_msg.edit_text("⏳ Загрузка коллекции из X...")
            files = []
            for i, item in enumerate(res.picker):
                if item.url:
                    ext = "mp4" if item.type in ("video", "gif") else "jpg"
                    path = await CobaltService.download_file(item.url, ext)
                    if path:
                        files.append((path, item.type))

            if not files:
                await status_msg.edit_text("⚠️ Не найдено медиа для загрузки.")
                return True

            await status_msg.edit_text(f"⏳ Отправка {len(files)} файлов из X...")
            success_count = 0
            for i, (path, item_type) in enumerate(files):
                is_photo = item_type == "photo"
                caption = f"🐦 X (Twitter) • {user.mention_html()}" if i == 0 else ""

                # Use standard send_file but correctly specify flags based on photo
                # TelegramSender.send_file behaves differently if we send a photo via send_video fallback
                # Wait, send_file only handles video, audio, gif. But wait!
                # If it's a photo, we should use send_photo
                # TelegramSender.send_file doesn't have is_photo flag, so it'll use bot.send_video.
                if is_photo:
                    try:
                        with open(path, "rb") as fh:
                            await context.bot.send_photo(
                                chat_id=chat.id,
                                photo=fh,
                                caption=caption,
                                parse_mode="HTML",
                            )
                            success_count += 1
                    except Exception as e:
                        logger.error("Failed to send X photo: %s", e)
                else:
                    success = await TelegramSender.send_file(
                        context.bot,
                        chat.id,
                        path,
                        is_audio=False,
                        is_gif=(item_type == "gif"),
                        caption=caption,
                        parse_mode="HTML",
                    )
                    if success:
                        success_count += 1

                try:
                    os.unlink(path)
                except Exception:
                    pass

            if success_count > 0:
                try:
                    await status_msg.delete()
                    await msg.delete()
                except Exception:
                    pass
            else:
                await status_msg.edit_text("⚠️ Ошибка отправки медиа с X.")

            return True

    except Exception as e:
        logger.error("Twitter Cobalt error: %s", e, exc_info=True)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return False

    return False
