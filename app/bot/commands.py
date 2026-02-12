from telegram import Update
from telegram.ext import ContextTypes
from app.constants import SUPPORTED_PLATFORMS
from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 <b>Привет! Я помогу скачать видео.</b>\n\n"
        "Просто отправь мне ссылку с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• VK / VK Video\n"
        "• RuTube\n"
        "• Pinterest\n\n"
        "<i>Я найду доступные форматы и отправлю видео прямо сюда.</i>"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "<b>Поддерживаемые сервисы:</b>\n"
        f"• {', '.join(sorted(SUPPORTED_PLATFORMS))}\n\n"
        "<b>Инструкция:</b>\n"
        "1. Скопируйте ссылку на видео.\n"
        "2. Отправьте ссылку боту.\n"
        "3. Выберите качество кнопок под сообщением.\n"
        "4. Выберите: получить ссылку или файл в Telegram.\n\n"
        f"❗️ <i>Если файл > {MAX_TG_UPLOAD_MB} МБ, он не сможет быть загружен в Telegram (ограничение API). Используйте прямую ссылку.</i>\n"
        f"📦 Максимальный размер для прямой загрузки: {MAX_DL_MB} МБ."
    )
    await update.message.reply_text(help_text, parse_mode="HTML")
