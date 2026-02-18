import logging
import uuid
import os
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import extract_supported_url
from app.services.downloader import MediaSender

logger = logging.getLogger("app.bot.group_logic")

# Build format string dynamically from config
_sz = f"{MAX_TG_UPLOAD_MB}M"
GROUP_VIDEO_FORMAT = f"bestvideo[ext=mp4][filesize<{_sz}]+bestaudio[ext=m4a]/best[ext=mp4][filesize<{_sz}]/best[filesize<{_sz}]"


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Optional: Check if we are allowed to speak?
    # Telegram bots can generally reply if they see the message.

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

    # Callback to update the status message
    async def update_ui(text, markup=None):
        try:
            # We use a simpler progress in groups to avoid spamming edits too much?
            # Or same processing.
            if status_msg:
                await status_msg.edit_text(text, reply_markup=markup)
        except Exception as e:
            logger.debug(f"Group UI update failed: {e}")

    # Prepare download task
    # We use a specific format for groups to ensure size < 45MB
    # We pass height=None so builders.py falls through to raw command.

    # Detect Pinterest to use a simpler format (Pinterest often lacks detailed metadata)
    is_pinterest = "pinterest" in url or "pin.it" in url

    if is_pinterest:
        # Relaxed format for Pinterest: just best video/audio, relying on max-filesize flag
        # Pinterest often has single stream, so 'best' is safer than forcing verify/merge
        video_format = "best[ext=mp4]/best"
    else:
        # Standard strict format for YouTube/TikTok
        video_format = GROUP_VIDEO_FORMAT

    file_path, error = await MediaSender.download_video(
        page_url=url,
        format_id=video_format,
        height=None,
        token=token,
        progress_callback=update_ui,
    )

    if error or not file_path:
        # If error, we might want to delete the status message or show error
        # In groups, clutter is bad. Show error for 5s then delete?
        try:
            await status_msg.edit_text(error or "❌ Ошибка.")
            # await asyncio.sleep(5)
            # await status_msg.delete()
        except:
            pass
        return

    # Success! Send video in Silent Mode (Delete original, Tag user)

    # 1. Delete original user message (Silent Mode)
    try:
        await update.message.delete()
    except Exception as e:
        logger.debug(f"Could not delete user message: {e}")

    # 2. Tag user in caption
    if update.effective_user.username:
        user_tag = f"@{update.effective_user.username}"
    else:
        user_tag = update.effective_user.mention_html()

    caption = f"👤 {user_tag}"

    await status_msg.edit_text("📤 Отправляю...")

    # Create "Send GIF" button
    # The callback data must include the token to find the file in cache
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎬 Send GIF", callback_data=f"gif|{token}")]]
    )

    success = await MediaSender.send_file(
        context.bot,
        update.effective_chat.id,
        file_path,
        is_audio=False,  # Prioritize video
        is_gif=False,
        caption=caption,
        reply_markup=kb,
        # reply_to_message_id=update.message.message_id # Cannot reply if deleted
    )

    if success:
        await status_msg.delete()
    else:
        await status_msg.edit_text("⚠️ Ошибка отправки.")

    # We do NOT remove the file here, because "Send GIF" needs it.
    # It remains in state.file_cache (TTLCache) until expiry.
