"""Bot command handlers.

Commands registered:
  /start       — welcome message
  /help        — usage information
  /mp3 <url>   — fast-path audio download (no format picker)
  /mp4 <url>   — fast-path best-quality video download (no format picker)
  /settings    — show current user preferences (or /settings reset to clear)
  /setformat   — set default format: video | audio
  /setquality  — set default quality: best | 1080 | 720 | 480 | 360
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.constants import AUDIO_FORMAT_ID, SUPPORTED_PLATFORMS
from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB
from app.core.texts import Texts
from app.core.utils import extract_url_from_update
from app.core.user_prefs import (
    get_prefs,
    set_prefs,
    clear_prefs,
    VALID_FORMATS,
    VALID_QUALITIES,
)

logger = logging.getLogger("app.bot.commands")

# Best video format for /mp4 — mirrors group_logic.py GROUP_VIDEO_FORMAT
_sz = f"{MAX_TG_UPLOAD_MB}M"
_MP4_FORMAT = (
    f"bestvideo[ext=mp4][filesize<{_sz}]+bestaudio[ext=m4a]"
    f"/best[ext=mp4][filesize<{_sz}]"
    f"/bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    f"/best[filesize<{_sz}]/best"
)


# ── /start ────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(Texts.WELCOME, parse_mode="HTML")


# ── /help ─────────────────────────────────────────────────────────────────────


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    help_text = Texts.HELP.format(
        platforms=", ".join(sorted(SUPPORTED_PLATFORMS)),
        max_tg_mb=MAX_TG_UPLOAD_MB,
        max_dl_mb=MAX_DL_MB,
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


# ── /mp3 and /mp4 fast-path helpers ──────────────────────────────────────────


async def _fast_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    format_id: str,
    is_audio: bool,
) -> None:
    """Shared implementation for /mp3 and /mp4: skip the format picker entirely."""
    msg = update.message
    user = update.effective_user
    chat = update.effective_chat
    assert msg is not None and user is not None and chat is not None

    # Resolve URL from the command text or from a replied-to message
    url, section = extract_url_from_update(msg)
    if not url:
        usage = Texts.CMD_MP3_USAGE if is_audio else Texts.CMD_MP4_USAGE
        await msg.reply_text(usage, parse_mode="HTML")
        return

    from app.core import state

    if not await state.limiter.allow_user(
        user.id
    ) or not await state.limiter.allow_chat(chat.id):
        await msg.reply_text(Texts.RATE_LIMITED)
        return

    try:
        await context.bot.send_chat_action(
            chat_id=chat.id,
            action=ChatAction.UPLOAD_VOICE if is_audio else ChatAction.UPLOAD_VIDEO,
        )
    except Exception:
        pass

    status_msg: Optional[Message] = await msg.reply_text(Texts.CMD_FAST_DL_START)

    token = uuid.uuid4().hex

    from app.core.models import DownloadContext
    from app.services.orchestrator import DownloadOrchestrator

    payload = DownloadContext(
        page_url=url,
        format_id=format_id,
        height=None,
        section=section,
    )

    async def _update_ui(text: str, markup: object = None) -> None:
        if status_msg:
            try:
                await status_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")  # type: ignore[arg-type]
            except Exception:
                pass

    success = await DownloadOrchestrator.process_download(
        token=token,
        chat_id=chat.id,
        bot=context.bot,
        payload=payload,
        fmt_size=None,
        update_ui=_update_ui,
        kb_error=None,
    )

    if success:
        try:
            await status_msg.delete()
        except Exception:
            pass


async def cmd_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mp3 <url> — download best audio, no format picker."""
    await _fast_download(
        update, context, format_id=AUDIO_FORMAT_ID, is_audio=True
    )


async def cmd_mp4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mp4 <url> — download best video, no format picker."""
    await _fast_download(
        update, context, format_id=_MP4_FORMAT, is_audio=False
    )


# ── /settings ─────────────────────────────────────────────────────────────────


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings [reset] — show or reset per-user download preferences."""
    msg = update.message
    user = update.effective_user
    assert msg is not None and user is not None

    args = context.args or []
    if args and args[0].lower() == "reset":
        await clear_prefs(user.id)
        await msg.reply_text(Texts.SETTINGS_RESET, parse_mode="HTML")
        return

    prefs = await get_prefs(user.id)
    fmt = prefs.get("default_format") or "—"
    q_raw = prefs.get("default_quality")
    quality = str(q_raw) + "p" if q_raw else (
        "наилучшее" if prefs.get("default_format") else "—"
    )

    await msg.reply_text(
        Texts.SETTINGS_HEADER.format(fmt=fmt, quality=quality),
        parse_mode="HTML",
    )


# ── /setformat ────────────────────────────────────────────────────────────────


async def cmd_setformat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setformat video|audio — persist the default download format."""
    msg = update.message
    user = update.effective_user
    assert msg is not None and user is not None

    args = context.args or []
    if not args or args[0].lower() not in VALID_FORMATS:
        await msg.reply_text(Texts.SETTINGS_INVALID_FMT, parse_mode="HTML")
        return

    value = args[0].lower()
    await set_prefs(user.id, default_format=value)
    await msg.reply_text(
        Texts.SETTINGS_FMT_SET.format(fmt=value), parse_mode="HTML"
    )


# ── /setquality ───────────────────────────────────────────────────────────────


async def cmd_setquality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setquality best|1080|720|480|360 — persist the default quality."""
    msg = update.message
    user = update.effective_user
    assert msg is not None and user is not None

    args = context.args or []
    if not args or args[0].lower() not in VALID_QUALITIES:
        await msg.reply_text(Texts.SETTINGS_INVALID_QUALITY, parse_mode="HTML")
        return

    key = args[0].lower()
    quality_px = VALID_QUALITIES[key]  # int or None
    await set_prefs(user.id, default_quality=quality_px)

    label = "наилучшее" if quality_px is None else f"{quality_px}p"
    await msg.reply_text(
        Texts.SETTINGS_QUALITY_SET.format(quality=label), parse_mode="HTML"
    )
