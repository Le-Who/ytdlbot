import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import FastAPI, Request
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.api.routes import router as api_router
from app.bot import callbacks, commands, messages
from app.core import config, state
from app.core.logging import set_correlation_id, setup_logging
from app.tasks.janitor import janitor_loop

setup_logging()
logger = logging.getLogger("app.main")


async def _global_error_handler(update, context):
    logger.error(
        "Unhandled exception in handler",
        exc_info=context.error,
        extra={"op": "error_handler"},
    )
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла внутренняя ошибка. Попробуйте позже."
            )
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting up...")
    logger.info("aria2c: %s", "enabled ✅" if state.ytdlp.has_aria2 else "not found ❌")

    # Redis lifecycle: verify connectivity at startup
    if state.redis_client:
        await state.redis_client.ping()
        logger.info("Redis connected ✅")

    stop_event = asyncio.Event()
    janitor_task = asyncio.create_task(janitor_loop(stop_event))

    bot_app = (
        Application.builder().token(config.BOT_TOKEN).concurrent_updates(True).build()
    )
    bot_app.add_handler(CommandHandler("start", commands.cmd_start))
    bot_app.add_handler(CommandHandler("help", commands.cmd_help))
    bot_app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            messages.on_message,
        )
    )

    from app.bot import group_logic

    bot_app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            group_logic.handle_group_message,
        )
    )

    bot_app.add_handler(CallbackQueryHandler(callbacks.on_back, pattern=r"^back$"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_pick, pattern=r"^pick\|"))
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_cancel, pattern=r"^cancel\|"))
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_cancel, pattern=r"^cancel_parse\|")
    )
    bot_app.add_handler(CallbackQueryHandler(callbacks.on_send, pattern=r"^send\|"))
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_convert_to_gif, pattern=r"^gif\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            callbacks.on_slideshow, pattern=r"^(slideshow|cbslide|apislide)\|"
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            group_logic.on_group_slideshow,
            pattern=r"^(grpslide|cbgrpslide|apigrpslide)\|",
        )
    )

    # ── Instagram callback handlers ──────────────────────────────────
    from app.bot import ig_callbacks

    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_stories, pattern=r"^ig_stories\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_highlights, pattern=r"^ig_highlights\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_highlight_items, pattern=r"^ig_hl_items\|"
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_download, pattern=r"^ig_dl\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_download_all, pattern=r"^ig_dl_all\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_hl_download, pattern=r"^ig_hl_dl\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_hl_download_all, pattern=r"^ig_hl_dl_all\|"
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_menu, pattern=r"^ig_menu\|")
    )

    bot_app.add_error_handler(_global_error_handler)

    await bot_app.initialize()
    await bot_app.start()
    state.bot_app = bot_app

    if config.WEBHOOK_URL:
        await bot_app.bot.set_webhook(
            url=f"{config.WEBHOOK_URL}/webhook",
            secret_token=config.TELEGRAM_SECRET_TOKEN,
            allowed_updates=["message", "callback_query"],
        )
    else:
        await bot_app.bot.delete_webhook()
        assert bot_app.updater is not None
        await bot_app.updater.start_polling(
            allowed_updates=["message", "callback_query"]
        )

    yield

    logger.info("Shutting down...")
    stop_event.set()
    await janitor_task
    if config.WEBHOOK_URL:
        await bot_app.bot.delete_webhook()
    else:
        assert bot_app.updater is not None
        await bot_app.updater.stop()

    await bot_app.stop()
    await bot_app.shutdown()

    # Redis lifecycle: clean close connection pool
    if state.redis_client:
        await state.redis_client.aclose()
        logger.info("Redis connection closed ✅")


api = FastAPI(lifespan=lifespan)
api.include_router(api_router)


@api.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Any:
    set_correlation_id(request.headers.get("X-Correlation-ID"))
    response = await call_next(request)
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
