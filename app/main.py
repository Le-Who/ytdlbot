import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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
    
    # Private chat messages
    bot_app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, messages.on_message)
    )
    
    # Group chat messages (importing inside function to avoid circular imports at top level if needed, or lazily)
    from app.bot import group_logic
    bot_app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_logic.handle_group_message)
    )

    bot_app.add_handler(CallbackQueryHandler(callbacks.on_back, pattern=r"^back$"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_pick, pattern=r"^pick\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_cancel, pattern=r"^cancel\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_send, pattern=r"^send\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_convert_to_gif, pattern=r"^gif\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_close, pattern=r"^close$"))

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
        logger.info("Webhook URL not found. Starting polling mode...")
        # Удаляем вебхук (если был) перед запуском поллинга
        await bot_app.bot.delete_webhook()
        # Запускаем поллинг в фоне
        await bot_app.updater.start_polling(
            allowed_updates=["message", "callback_query"]
        )

    yield

    # --- SHUTDOWN ---
    logger.info("Shutting down...")
    if config.WEBHOOK_URL:
        await bot_app.bot.delete_webhook()
    else:
        await bot_app.updater.stop()

    await bot_app.stop()
    await bot_app.shutdown()


# Инициализация FastAPI
api = FastAPI(lifespan=lifespan)
api.include_router(api_router)


@api.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # Relax CSP for API docs (FastAPI uses CDN for Swagger UI)
    if request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net data:"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'"

    return response
