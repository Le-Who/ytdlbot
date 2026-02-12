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

from app.core import config
from app.core import state
from app.core.logging import setup_logging, set_correlation_id
from app.api.routes import router as api_router
from app.bot import commands, messages, callbacks
from app.tasks.janitor import janitor_loop

setup_logging()
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...", extra={"op": "startup"})

    janitor_stop = asyncio.Event()
    janitor_task = asyncio.create_task(janitor_loop(janitor_stop))

    bot_app = Application.builder().token(config.BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", commands.cmd_start))
    bot_app.add_handler(CommandHandler("help", commands.cmd_help))
    bot_app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, messages.on_message)
    )

    from app.bot import group_logic
    bot_app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_logic.handle_group_message)
    )

    bot_app.add_handler(CallbackQueryHandler(callbacks.on_back, pattern=r"^back$"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_pick, pattern=r"^pick\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_cancel, pattern=r"^cancel\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_send, pattern=r"^send\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_convert_to_gif, pattern=r"^gif\|"))

    await bot_app.initialize()
    await bot_app.start()

    state.bot_app = bot_app

    if config.WEBHOOK_URL:
        logger.info(f"Setting webhook: {config.WEBHOOK_URL}/webhook")
        await bot_app.bot.set_webhook(
            url=f"{config.WEBHOOK_URL}/webhook",
            secret_token=config.TELEGRAM_SECRET_TOKEN,
            allowed_updates=["message", "callback_query"],
        )
    else:
        logger.info("Webhook URL not found. Starting polling mode...")
        await bot_app.bot.delete_webhook()
        await bot_app.updater.start_polling(allowed_updates=["message", "callback_query"])

    try:
        yield
    finally:
        logger.info("Shutting down...", extra={"op": "shutdown"})
        janitor_stop.set()
        await janitor_task
        if config.WEBHOOK_URL:
            await bot_app.bot.delete_webhook()
        else:
            await bot_app.updater.stop()

        await bot_app.stop()
        await bot_app.shutdown()


api = FastAPI(lifespan=lifespan)
api.include_router(api_router)


@api.middleware("http")
async def add_security_headers(request: Request, call_next):
    cid = set_correlation_id(request.headers.get("X-Correlation-Id"))
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net data:"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'"

    return response
