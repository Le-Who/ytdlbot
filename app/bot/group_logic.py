import logging
import uuid
import os
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.utils import extract_supported_url, check_rate_limit
from app.services.downloader import MediaSender
from app.constants import Platform, GROUP_VIDEO_FORMAT, GROUP_PINTEREST_FORMAT

logger = logging.getLogger("app.bot.group_logic")


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Passively monitors group messages for supported links.
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    url = extract_supported_url(text)

    # In groups, we only react if a URL is found (passive mode).
    if not url:
        return

    user = update.effective_user
    if not check_rate_limit(user.id, limit=3):
        return

    # Send "Uploading video..." action
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
            if status_msg:
                await status_msg.edit_text(text, reply_markup=markup)
        except Exception as e:
            logger.debug(f"Group UI update failed: {e}")

    # Detect platform for format selection
    platform = Platform.detect(url)

    if platform == Platform.PINTEREST:
        video_format = GROUP_PINTEREST_FORMAT
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
            await status_msg.edit_text(error or "❌ Ошибка.")
        except:
            pass
        return

    # Success! Send video (Silent Mode: Delete original, Tag user)
    try:
        await update.message.delete()
    except Exception as e:
        logger.debug(f"Could not delete user message: {e}")

    if update.effective_user.username:
        user_tag = f"@{update.effective_user.username}"
    else:
        user_tag = update.effective_user.mention_html()

    caption = f"👤 {user_tag}"

    await status_msg.edit_text("📤 Отправляю...")

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
        reply_markup=kb,
    )

    if success:
        await status_msg.delete()
    else:
        await status_msg.edit_text("⚠️ Ошибка отправки.")

    # We do NOT remove the file here, because "Send GIF" needs it.
    # It remains in state.file_cache (TTLCache) until expiry.
