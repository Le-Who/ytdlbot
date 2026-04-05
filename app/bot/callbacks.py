import os
import uuid
import asyncio
import logging
import html
from typing import Any
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
    "on_save_as_gif_file",
]

logger = logging.getLogger("app.bot.callbacks")


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
    except Exception as e:
        logger.warning("_edit_or_reply failed (editing caption/text): %s", e)
        # Ultimate fallback
        try:
            await msg.delete()
            await msg.chat.send_message(text, reply_markup=reply_markup, **kwargs)
        except Exception as e2:
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
        except Exception as e:
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
        _, pick_key = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
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
        ),
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
        _, token = q.data.split("|", 1)
        await state.cancel_cache.set(token, True)
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

    if not await state.limiter.allow_user(
        user_id
    ) or not await state.limiter.allow_chat(q.message.chat_id):
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
    payload = await state.link_cache.get(token)
    if not payload:
        await _edit_or_reply(q, Texts.LINK_EXPIRED)
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
            await q.edit_message_text(text, reply_markup=markup)  # type: ignore
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

    # Check/Add to processing set (Debounce) — operations on sets are atomic in asyncio
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
    from app.bot.keyboards import build_sent_gif_keyboard

    success = await MediaSender.send_file(
        context.bot,
        q.message.chat_id,
        gif_path,
        is_gif=True,
        reply_to_message_id=target_msg_id,
        caption="🎬 GIF",
        reply_markup=build_sent_gif_keyboard(token),
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

    if not await state.limiter.allow_user(
        user_id
    ) or not await state.limiter.allow_chat(q.message.chat_id):
        await _edit_or_reply(q, Texts.TOO_MANY_REQUESTS)
        return

    if not q.data:
        return

    try:
        prefix, rest = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error("Invalid callback data in on_slideshow", extra={"error": str(e)})
        return

    is_api = prefix == "apislide"

    if is_api:
        try:
            parse_token, mode = rest.split("|", 1)
        except ValueError:
            return

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
        from typing import Any

        api_res: Any = None
        if api_source == "tikwm":
            from app.services.tikwm import TikWMResult

            api_res = TikWMResult(**payload.api_json)
        elif api_source == "cobalt":
            from app.services.cobalt import CobaltResult

            api_res = CobaltResult(**payload.api_json)
        else:
            await _edit_or_reply(q, "⚠️ Неизвестный API источник.")
            return

        page_url = payload.page_url
    else:
        mode = rest
        data = context.user_data
        assert data is not None
        page_url = data.get("page_url")
        if not page_url:
            await _edit_or_reply(q, Texts.DATA_EXPIRED_RESEND)
            return
    if is_api:
        is_photo_mode = mode == "photo"
    else:
        is_photo_mode = mode == SLIDESHOW_PHOTO_FORMAT_ID
    if state.tasks_sem.locked():
        await _edit_or_reply(q, Texts.QUEUE_FULL)
        return

    await state.tasks_sem.acquire()
    try:
        await _edit_or_reply(q, Texts.SLIDESHOW_DOWNLOADING)

        if is_api:
            from app.services.gallery_dl.service import SlideshowResult

            from typing import Any

            image_paths: Any = []
            audio_path: Any = None

            if api_source == "tikwm":
                from app.services.tikwm import TikWMService

                image_paths, audio_path = await TikWMService.download_slideshow(api_res)
            elif api_source == "cobalt":
                from app.services.cobalt import CobaltService

                image_paths, audio_path = await CobaltService.download_slideshow(
                    api_res
                )

            if image_paths:
                result = SlideshowResult(images=image_paths, audio=audio_path)
                error = None
            else:
                result = None
                error = "⚠️ Ошибка загрузки слайдшоу из внешнего API."
        else:
            result, error = await MediaSender.download_slideshow(page_url)

        if error or not result:
            await _edit_or_reply(q, error or Texts.SLIDESHOW_ERROR)
            return

        try:
            if is_photo_mode:
                # Send as photo album
                await _edit_or_reply(q, Texts.SLIDESHOW_SENDING)

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
                    await _edit_or_reply(q, Texts.SEND_ERROR)
            else:
                # Convert to video and send
                await _edit_or_reply(q, Texts.SLIDESHOW_CONVERTING)

                video_path = await MediaSender.images_to_video(
                    result.images, result.audio
                )

                if not video_path:
                    await _edit_or_reply(q, Texts.SLIDESHOW_ERROR)
                    return

                await _edit_or_reply(q, Texts.SENDING_TO_TG)

                success = await MediaSender.send_file(
                    context.bot,
                    q.message.chat_id,
                    video_path,
                    caption="🎬",
                )

                if success:
                    await q.delete_message()
                else:
                    await _edit_or_reply(q, Texts.SEND_ERROR)

                # Cleanup video file
                await asyncio.to_thread(safe_remove, video_path)
        finally:
            # Always cleanup slideshow download directory
            await asyncio.to_thread(MediaSender.cleanup_slideshow, result)
    finally:
        state.tasks_sem.release()


async def on_save_as_gif_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles 'Save as .gif file' button press.

    On-demand native GIF export pipeline:
    1. Immediate Toast feedback + spinner button state.
    2. Check Redis cache for existing doc_file_id (instant re-send).
    3. Fetch source video path from file_cache (must still exist on disk).
       If missing: fall back to re-downloading from Telegram via bot.get_file().
    4. Convert to palettized .gif via convert_to_native_gif().
    5. Send as sendDocument (reply to the animation) — routes via Local API if configured.
    6. Cache the document.file_id in link_cache for future requests.
    7. Update button state to ✅ done.
    """
    from app.services.converter import MediaConverter
    from telegram import InputFile

    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data is not None

    _, token = q.data.split("|", 1)
    
    # Needs to get URL from context to determine global cache key
    ctx = await state.link_cache.get(token)
    page_url = None
    cache_key = f"gifdoc:{token}" # fallback
    if ctx and hasattr(ctx, "page_url"):
        page_url = ctx.page_url
        import hashlib
        h = hashlib.md5(page_url.encode()).hexdigest()
        cache_key = f"gifdoc_{h}"

    # -- 1. Immediate UX feedback --
    try:
        await q.answer(Texts.GIF_FILE_PREPARING_TOAST, show_alert=False)
    except Exception:
        pass

    # Spinner: update button label to show work in progress
    try:
        from app.bot.keyboards import build_sent_gif_keyboard
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        spinner_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(Texts.BTN_SAVE_GIF_WAIT, callback_data=f"giffile|{token}")]]
        )
        await q.message.edit_reply_markup(reply_markup=spinner_kb)
    except Exception as e:
        logger.debug("Spinner button update failed: %s", e)

    # -- 2. Check Global Redis cache for previously uploaded native GIF --
    cached_doc_id = await state.gifdoc_cache.get(cache_key)
    if cached_doc_id and isinstance(cached_doc_id, str):
        try:
            await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=cached_doc_id,
                caption="🎞 Нативный .gif файл",
                reply_to_message_id=q.message.message_id,
                read_timeout=60,
                write_timeout=60,
            )
            # Update button to done
            try:
                done_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(Texts.BTN_SAVE_GIF_DONE, callback_data=f"giffile|{token}")]]
                )
                await q.message.edit_reply_markup(reply_markup=done_kb)
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning("Cached gif doc_id send failed (%s), regenerating", e)

    # -- 3. Locate source video --
    # Try in-memory file_cache first (file is still on disk, fast path)
    video_path: str | None = state.file_cache.get(token)
    if video_path and not os.path.exists(video_path):
        video_path = None  # stale entry — evict implicitly

    if not video_path:
        # Fallback 1: re-download from Telegram using the animation's file_id
        # Local API serves this at full size with zero external traffic
        try:
            anim = getattr(q.message, "animation", None) or getattr(q.message, "video", None)
            if anim:
                tg_file = await context.bot.get_file(anim.file_id)
                tmp = os.path.join(
                    __import__("app.core.config", fromlist=["TEMP_DIR"]).TEMP_DIR,
                    f"gif_src_{token}.mp4",
                )
                await tg_file.download_to_drive(tmp)
                if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    video_path = tmp
                    logger.info("on_save_as_gif_file: re-fetched source from TG → %s", tmp)
        except Exception as e:
            logger.warning("on_save_as_gif_file: TG re-download failed: %s", e)

    if not video_path:
        try:
            await q.message.reply_text(Texts.GIF_FILE_EXPIRED, do_quote=True)
        except Exception:
            pass
        try:
            from app.bot.keyboards import build_sent_gif_keyboard
            await q.message.edit_reply_markup(reply_markup=build_sent_gif_keyboard(token))
        except Exception:
            pass
        return

    # -- Check size soft limit (50MB) to protect CPU and mobile clients --
    try:
        val_size = os.path.getsize(video_path)
        if val_size > 50 * 1024 * 1024:
            await q.answer("⚠️ Исходник слишком большой для генерации GIF (>50MB).", show_alert=True)
            return
    except OSError:
        pass

    # -- 4. Check if already being processed (debounce) --
    debounce_key = f"giffile_lock:{token}"
    if state.processing_gifs and debounce_key in state.processing_gifs:
        try:
            await q.answer("⏳ Конвертация уже идёт, подождите...", show_alert=False)
        except Exception:
            pass
        return
    if state.processing_gifs is not None:
        state.processing_gifs.add(debounce_key)

    gif_path: str | None = None
    tmp_src_created = (video_path and "gif_src_" in video_path)  # cleanup flag

    try:
        # -- Queue toast if semaphore is crowded --
        if state.gif_file_sem.locked():
            try:
                await q.answer(Texts.GIF_FILE_QUEUE_TOAST, show_alert=False)
            except Exception:
                pass

        # -- 5. Convert using palette-based native GIF export --
        if video_path.lower().endswith(".gif"):
            logger.info("on_save_as_gif_file: bypassing FFmpeg, source is already .gif (%s)", video_path)
            gif_path = video_path
        else:
            gif_path = await MediaConverter.convert_to_native_gif(video_path)

        if not gif_path:
            try:
                await q.message.reply_text(Texts.GIF_FILE_ERROR, do_quote=True)
            except Exception:
                pass
            return

        # -- 6. Send as document (reply to animation) --
        try:
            doc_input = InputFile(open(gif_path, "rb"), filename=f"animation_{token[:8]}.gif")

            sent = await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=doc_input,
                caption="🎞 Нативный .gif файл",
                reply_to_message_id=q.message.message_id,
                disable_content_type_detection=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )

            # -- 7. Cache the Telegram file_id purely by URL hash for 7-days instant future re-sends --
            if sent and sent.document:
                await state.gifdoc_cache.set(cache_key, sent.document.file_id)
                logger.info(
                    "on_save_as_gif_file: cached gif doc_file_id globally (%s)", cache_key
                )

            # Update button state to ✅ done
            try:
                done_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(Texts.BTN_SAVE_GIF_DONE, callback_data=f"giffile|{token}")]]
                )
                await q.message.edit_reply_markup(reply_markup=done_kb)
            except Exception:
                pass

        except Exception as e:
            logger.error("on_save_as_gif_file: send_document failed: %s", e, exc_info=True)
            try:
                await q.message.reply_text(Texts.GIF_FILE_ERROR, do_quote=True)
            except Exception:
                pass
    finally:
        state.processing_gifs.discard(debounce_key)
        if gif_path and gif_path != video_path:
            await asyncio.to_thread(safe_remove, gif_path)
        if tmp_src_created and video_path:
            await asyncio.to_thread(safe_remove, video_path)
