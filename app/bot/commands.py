from telegram import Update
from telegram.ext import ContextTypes
from app.constants import SUPPORTED_PLATFORMS
from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB
from app.core.texts import Texts


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    await update.message.reply_text(Texts.WELCOME, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message is not None
    help_text = Texts.HELP.format(
        platforms=", ".join(sorted(SUPPORTED_PLATFORMS)),
        max_tg_mb=MAX_TG_UPLOAD_MB,
        max_dl_mb=MAX_DL_MB,
    )
    await update.message.reply_text(help_text, parse_mode="HTML")
