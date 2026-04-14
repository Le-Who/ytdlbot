import asyncio
import logging
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import extract_url_from_update
from app.services.downloader import MediaSender
from app.core.models import DownloadContext
from app.core.texts import Texts
from app.services.ytdlp.parsers import _is_tiktok

logger = logging.getLogger("app.bot.group_logic")

# Build format string dynamically from config
_sz = f"{MAX_TG_UPLOAD_MB}M"
GROUP_VIDEO_FORMAT = (
    f"bestvideo[ext=mp4][filesize<{_sz}]+bestaudio[ext=m4a]"
    f"/best[ext=mp4][filesize<{_sz}]"
    f"/bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    f"/best[filesize<{_sz}]/best"
)


async def handle_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Passively monitors group messages for supported links.
    """
    if not update.message or not update.message.text:
        return

    url, _ = extract_url_from_update(update.message)

    # In groups, we only react if a URL is found.
    # We do NOT reply with error if URL is not supported (passive mode).
    if not url:
        return

    # Rate limiting for groups (per chat or per user?)
    # Let's limit per user to avoid spam.
    user = update.effective_user
    chat = update.effective_chat
    assert user is not None and chat is not None
    if not await state.limiter.allow_user(
        user.id
    ) or not await state.limiter.allow_chat(chat.id):
        return

    # Send "Typing..." or "Uploading video..." action
    await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.UPLOAD_VIDEO)

    # Initial status message
    status_msg = await update.message.reply_text("🔎")

    # Tag user in caption
    if user.username:
        user_tag = f"@{user.username}"
    else:
        user_tag = user.mention_html()

    # ── Instagram Early Intercept ────────────────────────────────────────
    from app.services.instagram import is_instagram_url

    if is_instagram_url(url):
        try:
            await status_msg.delete()
        except Exception:
            pass
        from app.bot.messages import _handle_instagram

        await _handle_instagram(update, context, url, uuid.uuid4().hex[:8], None)
        return

    # Generate token for this operation (used for file cache & callbacks)
    token = uuid.uuid4().hex

    # Check if this is a TikTok slideshow
    is_tiktok_url = _is_tiktok(url)
    is_slideshow = False
    tiktok_auth_error = False
    info_json_path = None  # cached extraction JSON for --load-info-json reuse
    is_tiktok_api_success = False
    from typing import Any

    tiktok_api_res: Any = None
    api_source = None

    if is_tiktok_url:
        from app.services.tikwm import TikWMService
        from app.core.config import ENABLE_COBALT_TIKTOK

        try:
            tikwm_res = await TikWMService.process(url)
            if tikwm_res.status in ("video", "picker"):
                is_tiktok_api_success = True
                tiktok_api_res = tikwm_res
                api_source = "tikwm"
                is_slideshow = tikwm_res.is_slideshow
        except Exception as exc:
            logger.warning("TikWM failed in group: %s", exc)

        if not is_tiktok_api_success and ENABLE_COBALT_TIKTOK:
            from app.services.cobalt import CobaltService

            try:
                cobalt_res = await CobaltService.process(url)
                if cobalt_res.status in ("tunnel", "redirect", "picker"):
                    is_tiktok_api_success = True
                    tiktok_api_res = cobalt_res
                    api_source = "cobalt"
                    is_slideshow = cobalt_res.is_slideshow
            except Exception as exc:
                logger.warning("Cobalt failed in group: %s", exc)

        # If TikTok APIs fail, bypass yt-dlp and force a GalleryDL fallback
        if not is_tiktok_api_success:
            if is_tiktok_url:
                from app.services.ytdlp.parsers import classify_tiktok_content

                logger.info(
                    "TikTok APIs failed in group, falling back to gallery-dl directly."
                )
                is_slideshow = classify_tiktok_content(url) == "slideshow"
                tiktok_auth_error = True
            else:
                try:
                    result = await state.ytdlp.list_formats(url)
                    is_slideshow = result.is_slideshow
                    tiktok_auth_error = result.tiktok_auth_error
                    info_json_path = result.info_json_path
                except Exception as exc:
                    logger.info("yt-dlp list_formats error in group: %s", exc)
                    is_slideshow = False

    # Route format processing
    is_pinterest = "pinterest" in url or "pin.it" in url
    if tiktok_auth_error:
        # We always want GalleryDL now for TikTok since it handles auth restrictions natively
        video_format = "gallerydl_fallback"
    else:
        # Detect Pinterest to use a simpler format
        if is_pinterest:
            video_format = "pinterest_native"
        else:
            video_format = GROUP_VIDEO_FORMAT

    if is_slideshow:
        # TikTok slideshow — offer format choice (album vs video)
        await state.link_cache.set(
            token,
            DownloadContext(
                page_url=url,
                user_tag=user_tag,
                chat_id=chat.id,
                original_msg_id=update.message.message_id,
                api_source=api_source,
                api_json=(
                    tiktok_api_res.__dict__
                    if is_tiktok_api_success and tiktok_api_res
                    else None
                ),
            ),
        )

        prefix = "apigrpslide" if is_tiktok_api_success else "grpslide"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📸 Альбом", callback_data=f"{prefix}|{token}|photo"
                    ),
                    InlineKeyboardButton(
                        "🎬 Видео", callback_data=f"{prefix}|{token}|video"
                    ),
                ]
            ]
        )
        await status_msg.edit_text(Texts.GROUP_SLIDESHOW_CHOICE, reply_markup=kb)
        return

    # Regular video download flow
    # Callback to update the status message
    async def update_ui(text, markup=None):
        try:
            if status_msg:
                await status_msg.edit_text(text, reply_markup=markup)
        except Exception:
            return

    _api_fmt = is_tiktok_url or is_pinterest or tiktok_auth_error
    _grp_queue = state.api_queue if _api_fmt else state.download_queue

    acquired = await _grp_queue.enqueue(update_ui)
    if not acquired:
        return

    try:
        if (
            is_tiktok_api_success
            and tiktok_api_res
            and not is_slideshow
            and tiktok_api_res.url
        ):
            from typing import Any

            if api_source == "tikwm":
                from app.services.tikwm import TikWMService

                file_path, err = await TikWMService.download_video(url)
            elif api_source == "cobalt":
                from app.services.cobalt import CobaltService

                file_path_raw = await CobaltService.download_file(
                    tiktok_api_res.url, "mp4"
                )
                file_path = str(file_path_raw) if file_path_raw else None
                err = "Cobalt API download failed" if not file_path else None
            
            if not file_path:
                logger.warning("Group TikTok fast-path failed (%s). Falling back to yt-dlp...", err)
                file_path, error = await MediaSender.download_video(
                    page_url=url,
                    format_id="bestvideo[vcodec^=avc]+bestaudio/best",
                    height=None,
                    token=token,
                    info_json_path=info_json_path,
                )
            else:
                error = None
                
            if file_path:
                state.file_cache[token] = file_path
        elif video_format == "gallerydl_fallback":
            from app.services.gallery_dl.service import GalleryDlService

            file_path, error = await asyncio.to_thread(
                GalleryDlService.download_video,
                url,
                state.ytdlp.cookies_path,
                state.ytdlp.tiktok_proxy,
            )
            if file_path:
                state.file_cache[token] = file_path
        elif video_format == "pinterest_native":
            from app.services.pinterest import PinterestNativeService

            file_path, error = await PinterestNativeService.download_video(url)
            if file_path:
                state.file_cache[token] = file_path
        else:
            file_path, error = await MediaSender.download_video(
                page_url=url,
                format_id=video_format,
                height=None,
                token=token,
                info_json_path=info_json_path,
            )

        if error or not file_path:
            try:
                await status_msg.edit_text(error or Texts.GROUP_ERROR)
            except Exception:
                pass
            return

        caption = f"👤 {user_tag}"

        await status_msg.edit_text(Texts.GROUP_SENDING)

        try:
            await context.bot.send_chat_action(
                chat_id=chat.id, action=ChatAction.UPLOAD_VIDEO
            )
        except Exception:
            pass

        from app.bot.keyboards import build_video_keyboard

        kb = build_video_keyboard(token)

        is_gif = isinstance(file_path, str) and file_path.lower().endswith(".gif")

        success = await MediaSender.send_file(
            context.bot,
            chat.id,
            file_path,
            is_audio=False,
            is_gif=is_gif,
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb,
        )
    finally:
        _grp_queue.release()

    if success:
        try:
            await update.message.delete()
        except Exception as e:
            logger.debug("Could not delete user message", extra={"error": str(e)})
        await status_msg.delete()
    else:
        await status_msg.edit_text(Texts.GROUP_SEND_ERROR)

    # File remains in state.file_cache (TTLCache) for GIF conversion reuse


async def on_group_slideshow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles group slideshow format choice (album or video) callback."""
    q = update.callback_query
    assert q is not None
    await q.answer()

    if not q.data:
        return

    try:
        prefix, token, mode = q.data.split("|", 2)
    except (ValueError, AttributeError) as e:
        logger.error(
            "Invalid callback data in on_group_slideshow", extra={"error": str(e)}
        )
        return

    payload = await state.link_cache.get(token)
    if not payload:
        try:
            await q.edit_message_text(Texts.SLIDESHOW_ERROR)
        except Exception:
            pass
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    page_url = payload.page_url
    user_tag = payload.user_tag
    chat_id = payload.chat_id
    original_msg_id = payload.original_msg_id
    is_photo_mode = mode == "photo"
    is_api = prefix == "apigrpslide"

    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass

    async def _grp_slide_ui(text: str, _markup=None) -> None:
        try:
            await q.edit_message_text(text)
        except Exception:
            pass

    acquired = await state.download_queue.enqueue(_grp_slide_ui)
    if not acquired:
        return

    try:
        await q.edit_message_text(Texts.SLIDESHOW_DOWNLOADING)

        if is_api and payload.api_json:
            from app.services.gallery_dl.service import SlideshowResult

            from typing import Any

            image_paths: Any = []
            audio_path = None
            error = None

            if payload.api_source == "tikwm":
                from app.services.tikwm import TikWMResult, TikWMService

                res: Any = TikWMResult(**payload.api_json)
                image_paths, audio_path = await TikWMService.download_slideshow(res)
            elif payload.api_source == "cobalt":
                from app.services.cobalt import CobaltResult, CobaltService

                res = CobaltResult(**payload.api_json)
                image_paths, audio_path = await CobaltService.download_slideshow(res)

            if image_paths:
                result = SlideshowResult(images=image_paths, audio=audio_path)
            else:
                result = None
                error = "⚠️ Ошибка загрузки слайдшоу из внешнего API."
        else:
            result, error = await MediaSender.download_slideshow(page_url)

        if error or not result:
            # Slideshow download failed — try TikWM as video fallback
            from app.services.tikwm import TikWMService

            logger.info(
                "Slideshow failed in group, trying TikWM fallback: %s", page_url
            )
            tikwm_path, tikwm_err = await TikWMService.download_video(page_url)
            if tikwm_path:
                # Got video via TikWM — send as video
                await q.edit_message_text(Texts.GROUP_SENDING)
                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
                    )
                except Exception:
                    pass

                caption = f"👤 {user_tag}"
                gif_token = uuid.uuid4().hex
                state.file_cache[gif_token] = tikwm_path
                from app.bot.keyboards import build_video_keyboard

                kb = build_video_keyboard(gif_token)
                success = await MediaSender.send_file(
                    context.bot,
                    chat_id,
                    tikwm_path,
                    is_audio=False,
                    is_gif=False,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                if success:
                    try:
                        await context.bot.delete_message(chat_id, original_msg_id)
                    except Exception:
                        pass
                    await q.delete_message()
                else:
                    await q.edit_message_text(Texts.GROUP_SEND_ERROR)
                return
            else:
                logger.warning("TikWM fallback also failed: %s", tikwm_err)
                await q.edit_message_text(error or Texts.SLIDESHOW_ERROR)
                return

        caption = f"👤 {user_tag}"

        try:
            if is_photo_mode:
                await q.edit_message_text(Texts.SLIDESHOW_SENDING)
                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO
                    )
                except Exception:
                    pass

                success = await MediaSender.send_slideshow_photos(
                    context.bot,
                    chat_id,
                    result.images,
                    caption=caption,
                    parse_mode="HTML",
                )
                if success:
                    try:
                        await context.bot.delete_message(chat_id, original_msg_id)
                    except Exception:
                        pass
                    await q.delete_message()
                else:
                    await q.edit_message_text(Texts.GROUP_SEND_ERROR)
            else:
                # Convert to video
                await q.edit_message_text(Texts.SLIDESHOW_CONVERTING)

                video_path = await MediaSender.images_to_video(
                    result.images, result.audio
                )
                if not video_path:
                    await q.edit_message_text(Texts.SLIDESHOW_ERROR)
                    return

                await q.edit_message_text(Texts.GROUP_SENDING)
                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
                    )
                except Exception:
                    pass

                gif_token = uuid.uuid4().hex
                state.file_cache[gif_token] = video_path
                from app.bot.keyboards import build_video_keyboard

                kb = build_video_keyboard(gif_token)
                success = await MediaSender.send_file(
                    context.bot,
                    chat_id,
                    video_path,
                    is_audio=False,
                    is_gif=False,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                if success:
                    try:
                        await context.bot.delete_message(chat_id, original_msg_id)
                    except Exception:
                        pass
                    await q.delete_message()
                else:
                    await q.edit_message_text(Texts.GROUP_SEND_ERROR)

                # Cleanup video file (not cached for GIF, slideshow video is one-off)
                # Actually, we cached it above for GIF. Don't remove.
        finally:
            await asyncio.to_thread(MediaSender.cleanup_slideshow, result)
    finally:
        state.download_queue.release()
