import asyncio
import logging
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import extract_supported_url
from app.services.downloader import MediaSender
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


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Passively monitors group messages for supported links.
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    url = extract_supported_url(text)

    # In groups, we only react if a URL is found.
    # We do NOT reply with error if URL is not supported (passive mode).
    if not url:
        return

    # Rate limiting for groups (per chat or per user?)
    # Let's limit per user to avoid spam.
    user = update.effective_user
    chat = update.effective_chat
    assert user is not None and chat is not None
    if not state.limiter.allow_user(user.id) or not state.limiter.allow_chat(
        chat.id
    ):
        return

    # Send "Typing..." or "Uploading video..." action
    await context.bot.send_chat_action(
        chat_id=chat.id, action=ChatAction.UPLOAD_VIDEO
    )

    # Initial status message
    status_msg = await update.message.reply_text("🔎")

    # Generate token for this operation (used for file cache & callbacks)
    token = uuid.uuid4().hex

    # Tag user in caption
    if user.username:
        user_tag = f"@{user.username}"
    else:
        user_tag = user.mention_html()

    # Check if this is a TikTok slideshow
    is_tiktok_url = _is_tiktok(url)
    is_slideshow = False
    tiktok_auth_error = False  # age-restricted content needing TikWM fallback

    if is_tiktok_url:
        # Fast path: /photo/ URLs are always slideshows (no extraction needed)
        if "/photo/" in url:
            is_slideshow = True
        else:
            try:
                info = await asyncio.to_thread(state.ytdlp.extract, url, True)
                raw_formats = info.get("formats", [])
                has_video = any(
                    fmt.get("vcodec") not in (None, "none")
                    for fmt in raw_formats
                )
                is_slideshow = not has_video
            except Exception as exc:
                err_msg = str(exc).lower()
                if "log in" in err_msg or "cookies" in err_msg or "sign in" in err_msg:
                    # Auth/age-restricted — NOT a slideshow, use TikWM directly
                    logger.info("TikTok auth error in group, will use TikWM: %s", url)
                    tiktok_auth_error = True
                elif "unsupported url" in err_msg:
                    is_slideshow = True
                else:
                    # Unknown error — assume slideshow as fallback
                    is_slideshow = True

    # TikTok auth-restricted video → bypass slideshow UI, go straight to TikWM
    if tiktok_auth_error:
        from app.services.tikwm import TikWMService
        await status_msg.edit_text(Texts.GROUP_SENDING)
        tikwm_path, tikwm_err = await asyncio.to_thread(
            TikWMService.download_video, url
        )
        if tikwm_path:
            caption = f"👤 {user_tag}"
            gif_token = uuid.uuid4().hex
            state.file_cache[gif_token] = tikwm_path
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{gif_token}")]]
            )
            try:
                await context.bot.send_chat_action(
                    chat_id=chat.id, action=ChatAction.UPLOAD_VIDEO
                )
            except Exception:
                pass
            success = await MediaSender.send_file(
                context.bot, chat.id, tikwm_path,
                is_audio=False, is_gif=False,
                caption=caption, parse_mode="HTML", reply_markup=kb,
            )
            if success:
                try:
                    await update.message.delete()
                except Exception:
                    pass
                await status_msg.delete()
            else:
                await status_msg.edit_text(Texts.GROUP_SEND_ERROR)
        else:
            logger.warning("TikWM fallback failed for auth-restricted: %s", tikwm_err)
            await status_msg.edit_text(Texts.GROUP_ERROR)
        return

    if is_slideshow:
        # TikTok slideshow — offer format choice (album vs video)
        state.link_cache[token] = {
            "page_url": url,
            "user_tag": user_tag,
            "chat_id": chat.id,
            "original_msg_id": update.message.message_id,
        }

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📸 Альбом", callback_data=f"grpslide|{token}|photo"),
                InlineKeyboardButton("🎬 Видео", callback_data=f"grpslide|{token}|video"),
            ]
        ])
        await status_msg.edit_text(Texts.GROUP_SLIDESHOW_CHOICE, reply_markup=kb)
        return

    # Regular video download flow
    # Callback to update the status message
    async def update_ui(text, markup=None):
        try:
            if status_msg:
                await status_msg.edit_text(text, reply_markup=markup)
        except Exception as e:
            logger.debug(f"Group UI update failed: {e}")

    # Detect Pinterest to use a simpler format
    is_pinterest = "pinterest" in url or "pin.it" in url

    if is_pinterest:
        video_format = "best[ext=mp4]/best"
    else:
        video_format = GROUP_VIDEO_FORMAT

    file_path, error = await MediaSender.download_video(
        page_url=url,
        format_id=video_format,
        height=None,
        token=token,
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

    # Create "Send GIF" button
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{token}")]]
    )

    success = await MediaSender.send_file(
        context.bot,
        chat.id,
        file_path,
        is_audio=False,
        is_gif=False,
        caption=caption,
        parse_mode="HTML",
        reply_markup=kb,
    )

    if success:
        try:
            await update.message.delete()
        except Exception as e:
            logger.debug(f"Could not delete user message: {e}")
        await status_msg.delete()
    else:
        await status_msg.edit_text(Texts.GROUP_SEND_ERROR)

    # File remains in state.file_cache (TTLCache) for GIF conversion reuse


async def on_group_slideshow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles group slideshow format choice (album or video) callback."""
    q = update.callback_query
    assert q is not None
    await q.answer()

    if not q.data:
        return

    try:
        _, token, mode = q.data.split("|", 2)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_group_slideshow: {e}")
        return

    payload = state.link_cache.get(token)
    if not payload:
        try:
            await q.edit_message_text(Texts.SLIDESHOW_ERROR)
        except Exception:
            pass
        return

    page_url = payload["page_url"]
    user_tag = payload["user_tag"]
    chat_id = payload["chat_id"]
    original_msg_id = payload.get("original_msg_id")
    is_photo_mode = mode == "photo"

    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass

    await q.edit_message_text(Texts.SLIDESHOW_DOWNLOADING)

    result, error = await MediaSender.download_slideshow(page_url)

    if error or not result:
        # Slideshow download failed — try TikWM as video fallback
        from app.services.tikwm import TikWMService
        logger.info("Slideshow failed in group, trying TikWM fallback: %s", page_url)
        tikwm_path, tikwm_err = await asyncio.to_thread(
            TikWMService.download_video, page_url
        )
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
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{gif_token}")]]
            )
            success = await MediaSender.send_file(
                context.bot, chat_id, tikwm_path,
                is_audio=False, is_gif=False,
                caption=caption, parse_mode="HTML", reply_markup=kb,
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
                context.bot, chat_id, result.images,
                caption=caption, parse_mode="HTML",
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
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{gif_token}")]]
            )
            success = await MediaSender.send_file(
                context.bot, chat_id, video_path,
                is_audio=False, is_gif=False,
                caption=caption, parse_mode="HTML", reply_markup=kb,
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
