import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Core imports
from app.core import config
from app.core import state
from app.api.routes import router as api_router
from app.bot import commands, messages, callbacks

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    logger.info("Starting up...")
    
    # Инициализация Telegram Bot
    bot_app = Application.builder().token(config.BOT_TOKEN).build()
    
    # Регистрация хендлеров
    bot_app.add_handler(CommandHandler("start", commands.cmd_start))
    bot_app.add_handler(CommandHandler("help", commands.cmd_help))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages.on_message))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_back, pattern=r"^back$"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_pick, pattern=r"^pick\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_cancel, pattern=r"^cancel\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_send, pattern=r"^send\|"))

    # Инициализация бота
    await bot_app.initialize()
    await bot_app.start()

    # Сохраняем ссылку в state для использования в вебхуках
    state.bot_app = bot_app

    # Настройка Webhook или Polling
    if config.WEBHOOK_URL:
        logger.info(f"Setting webhook: {config.WEBHOOK_URL}/webhook")
        await bot_app.bot.set_webhook(
            url=f"{config.WEBHOOK_URL}/webhook",
            secret_token=config.TELEGRAM_SECRET_TOKEN,
            allowed_updates=["message", "callback_query"],
        )
    else:
        logger.info("Webhook URL not found. Polling mode is not implemented in this refactor (assuming webhook).")
        # Для поллинга нужно запускать bot_app.updater.start_polling(), но в режиме FastAPI
        # обычно используется вебхук. Если нужен поллинг, это можно добавить отдельным таском.
        pass

    yield

    # --- SHUTDOWN ---
    logger.info("Shutting down...")
    if config.WEBHOOK_URL:
        await bot_app.bot.delete_webhook()
    
    await bot_app.stop()
    await bot_app.shutdown()

# Инициализация FastAPI
api = FastAPI(lifespan=lifespan)
api.include_router(api_router)
