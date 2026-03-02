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
GROUP_VIDEO_FORMAT = f"bestvideo[ext=mp4][filesize<{_sz}]+bestaudio[ext=m4a]/best[ext=mp4][filesize<{_sz}]/best[filesize<{_sz}]"


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
    if not state.limiter.allow_user(user.id) or not state.limiter.allow_chat(
        update.effective_chat.id
    ):
        return

    # Send "Typing..." or "Uploading video..." action
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO
    )

    # Initial status message
    status_msg = await update.message.reply_text("🔎")

    # Generate token for this operation (used for file cache & callbacks)
    token = uuid.uuid4().hex

    # Tag user in caption
    if update.effective_user.username:
        user_tag = f"@{update.effective_user.username}"
    else:
        user_tag = update.effective_user.mention_html()

    # Check if this is a TikTok slideshow
    is_tiktok_url = _is_tiktok(url)
    is_slideshow = False

    if is_tiktok_url:
        try:
            info = await asyncio.to_thread(state.ytdlp.extract, url, True)
            raw_formats = info.get("formats", [])
            has_video = any(
                fmt.get("vcodec") not in (None, "none")
                for fmt in raw_formats
            )
            is_slideshow = not has_video
        except Exception:
            # If extraction fails for TikTok, assume it might be a slideshow
            is_slideshow = True

    if is_slideshow:
        # TikTok slideshow — download and send as photo album
        await status_msg.edit_text(Texts.SLIDESHOW_DOWNLOADING)

        result, error = await MediaSender.download_slideshow(url)

        if error or not result:
            try:
                await status_msg.edit_text(error or Texts.SLIDESHOW_ERROR)
            except Exception:
                pass
            return

        caption = f"👤 {user_tag}"

        await status_msg.edit_text(Texts.SLIDESHOW_SENDING)

        try:
            success = await MediaSender.send_slideshow_photos(
                context.bot,
                update.effective_chat.id,
                result.images,
                caption=caption,
                parse_mode="HTML",
            )

            if success:
                try:
                    await update.message.delete()
                except Exception as e:
                    logger.debug(f"Could not delete user message: {e}")
                await status_msg.delete()
            else:
                await status_msg.edit_text(Texts.GROUP_SEND_ERROR)
        finally:
            await asyncio.to_thread(MediaSender.cleanup_slideshow, result)
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
        progress_callback=update_ui,
    )

    if error or not file_path:
        try:
            await status_msg.edit_text(error or Texts.GROUP_ERROR)
        except Exception:
            pass
        return

    caption = f"👤 {user_tag}"

    await status_msg.edit_text(Texts.GROUP_SENDING)

    # Create "Send GIF" button
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{token}")]]
    )

    success = await MediaSender.send_file(
        context.bot,
        update.effective_chat.id,
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
