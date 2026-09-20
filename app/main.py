import asyncio
import html
import logging
import os
import traceback
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.api.routes import configure_job_store
from app.api.routes import router as api_router
from app.bot import callbacks, commands, messages
from app.core import config, state
from app.core.drain import DrainController, DurableUpdateWorker
from app.core.job_store import JobStore, mark_current_job_failed
from app.core.logging import set_correlation_id, setup_logging
from app.tasks.janitor import janitor_loop

setup_logging()
logger = logging.getLogger("app.main")


async def stop_telegram_ingress(bot_app: Any, *, webhook_enabled: bool) -> None:
    """Stop polling while preserving a webhook across routine deployments."""

    if webhook_enabled:
        return
    assert bot_app.updater is not None
    await bot_app.updater.stop()


async def shutdown_runtime(
    *,
    drain_controller: DrainController,
    durable_worker: DurableUpdateWorker,
    stop_event: asyncio.Event,
    background_tasks: tuple[asyncio.Task[None], ...],
    bot_app: Any,
    webhook_enabled: bool,
    job_store: JobStore,
    redis_client: Any,
    drain_timeout_seconds: float,
) -> None:
    """Attempt every shutdown phase even when an earlier phase fails."""

    async def attempt(
        label: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            await operation()
        except Exception as error:
            logger.error(
                "Shutdown phase failed",
                extra={"phase": label, "error_type": type(error).__name__},
            )

    await attempt(
        "durable drain",
        lambda: drain_controller.drain(deadline_seconds=drain_timeout_seconds),
    )
    await attempt("durable worker", durable_worker.stop)
    stop_event.set()
    background_results = await asyncio.gather(
        *background_tasks,
        return_exceptions=True,
    )
    for task, result in zip(background_tasks, background_results, strict=True):
        if isinstance(result, BaseException):
            logger.error(
                "Background task failed during shutdown",
                extra={"task": task.get_name(), "error_type": type(result).__name__},
            )
    await attempt(
        "Telegram ingress",
        lambda: stop_telegram_ingress(
            bot_app,
            webhook_enabled=webhook_enabled,
        ),
    )
    await attempt("Telegram application stop", bot_app.stop)
    await attempt("Telegram application shutdown", bot_app.shutdown)

    state.media_pipeline = None
    state.bot_app = None
    configure_job_store(None)
    await attempt("job store close", job_store.close)
    if redis_client:
        await attempt("Redis close", redis_client.aclose)
        logger.info("Redis connection closed ✅")


def _build_telegram_application_builder() -> Any:
    """Build PTB with one explicit cloud or required Local Bot API profile."""
    builder = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .media_write_timeout(config.TELEGRAM_MEDIA_WRITE_TIMEOUT)
        .read_timeout(config.TELEGRAM_READ_TIMEOUT)
        .write_timeout(config.TELEGRAM_WRITE_TIMEOUT)
        .connect_timeout(config.TELEGRAM_CONNECT_TIMEOUT)
        .pool_timeout(config.TELEGRAM_POOL_TIMEOUT)
    )
    if config.TELEGRAM_LOCAL_ENDPOINT:
        endpoint = config.TELEGRAM_LOCAL_ENDPOINT.rstrip("/")
        builder = (
            builder.base_url(f"{endpoint}/bot")
            .base_file_url(f"{endpoint}/file/bot")
            .local_mode(True)
        )
    return builder


async def _global_error_handler(update, context):
    mark_current_job_failed(context.error)
    logger.error(
        "Unhandled exception in handler",
        exc_info=context.error,
        extra={"op": "error_handler"},
    )

    # ── Notify user ───────────────────────────────────────────────
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Произошла внутренняя ошибка. Попробуйте позже."
            )
    except Exception:
        pass

    # ── Notify admin ──────────────────────────────────────────────
    if not config.ADMIN_CHAT_ID:
        return

    try:
        # Format traceback (capped to keep within 4096 TG limit)
        tb_lines = traceback.format_exception(
            type(context.error), context.error, context.error.__traceback__
        )
        tb_text = "".join(tb_lines)
        if len(tb_text) > 3500:
            tb_text = "...(truncated)\n" + tb_text[-3500:]

        # Extract useful update context
        ctx_lines: list[str] = []
        if update:
            if update.effective_user:
                u = update.effective_user
                ctx_lines.append(f"👤 User: {u.full_name} (@{u.username}) id={u.id}")
            if update.effective_chat:
                c = update.effective_chat
                ctx_lines.append(f"💬 Chat: {c.title or c.type} id={c.id}")
            msg = update.effective_message
            if msg and msg.text:
                preview = msg.text[:200]
                ctx_lines.append(f"📨 Message: {preview!r}")

        ctx_text = "\n".join(ctx_lines) or "No update context"

        report = (
            f"🔴 <b>Unhandled exception</b>\n\n"
            f"{html.escape(ctx_text)}\n\n"
            f"<pre>{html.escape(tb_text)}</pre>"
        )

        await context.bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            text=report,
            parse_mode="HTML",
        )
    except Exception as notify_err:
        logger.warning("Failed to send error report to admin: %s", notify_err)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting up...")
    logger.info("aria2c: %s", "enabled ✅" if state.ytdlp.has_aria2 else "not found ❌")

    # Cookie diagnostics — re-emit after logging is configured (cookies init at import time)
    _cm = state.ytdlp.cookies_manager
    _loaded = [k for k, v in _cm._platform_cookies.items() if v]
    if _cm._global_cookies_path:
        logger.info("Cookies: global ✅ | platforms loaded: %s", _loaded or "none")
    elif _loaded:
        logger.info("Cookies: platforms loaded ✅: %s", _loaded)
    else:
        logger.warning("Cookies: none configured ⚠️  (VK/FB/TT downloads will fail)")

    job_store = JobStore()
    drain_controller = DrainController(job_store)
    stop_event = asyncio.Event()
    background_tasks: tuple[asyncio.Task[None], ...] = ()

    app_builder = _build_telegram_application_builder()

    if config.TELEGRAM_LOCAL_ENDPOINT:
        logger.info(
            "Using local Telegram Bot API Server at %s", config.TELEGRAM_LOCAL_ENDPOINT
        )

    bot_app = app_builder.build()
    bot_app.add_handler(CommandHandler("start", commands.cmd_start))
    bot_app.add_handler(CommandHandler("help", commands.cmd_help))
    bot_app.add_handler(CommandHandler("mp3", commands.cmd_mp3))
    bot_app.add_handler(CommandHandler("mp4", commands.cmd_mp4))
    bot_app.add_handler(CommandHandler("settings", commands.cmd_settings))
    bot_app.add_handler(CommandHandler("setformat", commands.cmd_setformat))
    bot_app.add_handler(CommandHandler("setquality", commands.cmd_setquality))
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

    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_back, pattern=r"^(?:m2\|)?back$")
    )
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_pick, pattern=r"^(?:m2\|)?pick\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_cancel, pattern=r"^(?:m2\|)?cancel\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_cancel, pattern=r"^(?:m2\|)?cancel_parse\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_send, pattern=r"^(?:m2\|)?send\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(callbacks.on_convert_to_gif, pattern=r"^(?:m2\|)?gif\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            callbacks.on_save_as_gif_file, pattern=r"^(?:m2\|)?giffile\|"
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            callbacks.on_slideshow,
            pattern=r"^(?:m2\|)?(?:slideshow|cbslide|apislide)\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            group_logic.on_group_slideshow,
            pattern=r"^(?:m2\|)?(?:grpslide|cbgrpslide|apigrpslide)\|",
        )
    )

    # ── Instagram callback handlers ──────────────────────────────────
    from app.bot import ig_callbacks

    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_stories, pattern=r"^(?:m2\|)?ig_stories\|"
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_highlights,
            pattern=r"^(?:m2\|)?ig_highlights\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_highlight_items,
            pattern=r"^(?:m2\|)?ig_hl_items\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_download, pattern=r"^(?:m2\|)?ig_dl\|")
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_download_all,
            pattern=r"^(?:m2\|)?ig_dl_all\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_hl_download,
            pattern=r"^(?:m2\|)?ig_hl_dl\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(
            ig_callbacks.on_ig_hl_download_all,
            pattern=r"^(?:m2\|)?ig_hl_dl_all\|",
        )
    )
    bot_app.add_handler(
        CallbackQueryHandler(ig_callbacks.on_ig_menu, pattern=r"^(?:m2\|)?ig_menu\|")
    )

    bot_app.add_error_handler(_global_error_handler)

    async def process_durable_update(payload: dict[str, Any]) -> None:
        update = Update.de_json(payload, bot_app.bot)
        await bot_app.process_update(update)

    durable_worker = DurableUpdateWorker(
        job_store,
        drain_controller,
        process_durable_update,
    )

    try:
        # Acquire runtime resources only after synchronous application setup so
        # builder/handler failures cannot strand tasks or durable ownership.
        if state.redis_client:
            await state.redis_client.ping()
            logger.info("Redis connected ✅")
        await job_store.initialize()
        janitor_task = asyncio.create_task(janitor_loop(stop_event))
        background_tasks = (janitor_task,)

        await bot_app.initialize()
        from app.services.media.pipeline import build_default_pipeline

        state.media_pipeline = build_default_pipeline(bot_app.bot)
        await bot_app.start()
        state.bot_app = bot_app
        configure_job_store(job_store)
        await durable_worker.start()

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
    finally:
        logger.info("Shutting down...")
        await shutdown_runtime(
            drain_controller=drain_controller,
            durable_worker=durable_worker,
            stop_event=stop_event,
            background_tasks=background_tasks,
            bot_app=bot_app,
            webhook_enabled=bool(config.WEBHOOK_URL),
            job_store=job_store,
            redis_client=state.redis_client,
            drain_timeout_seconds=float(os.getenv("DRAIN_TIMEOUT_SECONDS", "30")),
        )


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
