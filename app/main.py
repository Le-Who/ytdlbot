import os
import re
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

from .ytdlp_service import YtDlpService

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "30"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "0").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

if not BASE_URL:
    BASE_URL = "http://localhost:8000"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app")

api = FastAPI()
ytdlp = YtDlpService()
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# token -> payload (page_url, format_id, title)
link_cache: TTLCache = TTLCache(maxsize=2000, ttl=LINK_TTL_MINUTES * 60)

URL_RE = re.compile(r"^https?://", re.I)


def is_supported_url(text: str) -> bool:
    if not URL_RE.search(text or ""):
        return False
    t = text.lower()
    return (
        ("youtube.com" in t)
        or ("youtu.be" in t)
        or ("rutube.ru" in t)
        or ("vk.com" in t)
        or ("vkvideo.ru" in t)
    )


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/favicon.ico")
async def favicon():
    raise HTTPException(status_code=404, detail="No favicon")


@api.get("/dl/{token}")
async def download(token: str):
    logger.info(f"[DOWNLOAD] Token: {token}")
    
    payload = link_cache.get(token)
    if not payload:
        logger.warning(f"[DOWNLOAD] Token expired: {token}")
        raise HTTPException(status_code=404, detail="Link expired or not found")

    page_url = payload["page_url"]
    format_id = payload["format_id"]
    title = payload.get("title") or "video"
    
    logger.info(f"[DOWNLOAD] URL: {page_url}, Format: {format_id}")

    try:
        logger.info("[DOWNLOAD] Extracting direct URL from yt-dlp...")
        direct_url, headers = await asyncio.to_thread(ytdlp.get_direct_url, page_url, format_id)
        logger.info(f"[DOWNLOAD] Direct URL obtained: {direct_url[:100]}...")
        
        logger.info("[DOWNLOAD] Getting cookies...")
        cookies = await asyncio.to_thread(ytdlp.get_cookies_dict)
        logger.info(f"[DOWNLOAD] Cookies count: {len(cookies)}")
    except Exception as e:
        logger.error(f"[DOWNLOAD] Extractor error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Extractor error: {e}")

    timeout = aiohttp.ClientTimeout(total=60 * 60)
    
    logger.info("[DOWNLOAD] Starting aiohttp session...")
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers or {}, cookies=cookies) as session:
            logger.info(f"[DOWNLOAD] Requesting: {direct_url[:80]}...")
            async with session.get(direct_url, allow_redirects=True) as resp:
                logger.info(f"[DOWNLOAD] Response status: {resp.status}")
                
                if resp.status >= 400:
                    body = ""
                    try:
                        body = (await resp.text())[:500]
                    except Exception:
                        pass
                    logger.error(f"[DOWNLOAD] Upstream error {resp.status}: {body}")
                    raise HTTPException(
                        status_code=502,
                        detail=f"YouTube returned {resp.status}. {body[:200]}",
                    )

                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                cd = f'attachment; filename="{title}.mp4"'
                
                logger.info("[DOWNLOAD] Starting streaming to client...")

                async def gen():
                    try:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            yield chunk
                    except Exception as e:
                        logger.error(f"[DOWNLOAD] Streaming error: {e}")

                return StreamingResponse(
                    gen(),
                    media_type=content_type,
                    headers={"Content-Disposition": cd},
                )
    except aiohttp.ClientError as e:
        logger.error(f"[DOWNLOAD] aiohttp error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Network error: {e}")
    except Exception as e:
        logger.error(f"[DOWNLOAD] Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Server error: {e}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправьте ссылку на видео (YouTube / VK Видео / RuTube).\n"
        "Дальше выберите качество и получите ссылку на скачивание."
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not is_supported_url(text):
        await update.message.reply_text("Пришлите ссылку на YouTube / VK Видео / RuTube.")
        return

    msg = await update.message.reply_text("Анализирую видео…")
    try:
        logger.info(f"[BOT] Parsing video: {text}")
        title, formats, audio = await asyncio.to_thread(ytdlp.list_formats, text)
        logger.info(f"[BOT] Video parsed: {title}, {len(formats)} formats")
    except Exception as e:
        logger.error(f"[BOT] Parse error: {e}", exc_info=True)
        await msg.edit_text(f"Ошибка парсинга: {e}")
        return

    context.user_data["page_url"] = text
    context.user_data["title"] = title

    buttons = []
    for f in formats:
        buttons.append([InlineKeyboardButton(f.label, callback_data=f"pick|{f.format_id}")])

    buttons.append([InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")])

    await msg.edit_text(
        f"Название: {title}\nВыберите качество/вариант:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        _, format_id = q.data.split("|", 1)
    except Exception:
        await q.edit_message_text("Некорректный выбор.")
        return

    page_url = context.user_data.get("page_url")
    title = context.user_data.get("title", "video")
    if not page_url:
        await q.edit_message_text("Сессия устарела. Пришлите ссылку заново.")
        return

    token = uuid.uuid4().hex
    link_cache[token] = {"page_url": page_url, "format_id": format_id, "title": title}
    dl_link = f"{BASE_URL}/dl/{token}"
    
    logger.info(f"[BOT] Generated link: {dl_link}")

    kb = [[InlineKeyboardButton("Открыть ссылку", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append([InlineKeyboardButton("Отправить документ (если небольшой)", callback_data=f"send|{token}")])

    await q.edit_message_text(
        f"Ссылка на скачивание (действует ~{LINK_TTL_MINUTES} мин):\n{dl_link}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
    )


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        _, token = q.data.split("|", 1)
    except Exception:
        await q.edit_message_text("Некорректная команда.")
        return

    payload = link_cache.get(token)
    if not payload:
        await q.edit_message_text("Ссылка устарела. Получите ссылку заново и попробуйте снова.")
        return

    page_url = payload["page_url"]
    format_id = payload["format_id"]
    title = payload.get("title") or "video"

    async with tasks_sem:
        await q.edit_message_text("Готовлю файл для отправки в Telegram…")
        
        logger.info(f"[BOT] Telegram upload started for: {page_url}")

        try:
            direct_url, headers = await asyncio.to_thread(ytdlp.get_direct_url, page_url, format_id)
            cookies = await asyncio.to_thread(ytdlp.get_cookies_dict)
            logger.info(f"[BOT] Direct URL obtained for upload")
        except Exception as e:
            logger.error(f"[BOT] Upload extract error: {e}", exc_info=True)
            await q.edit_message_text(f"Не удалось получить прямую ссылку: {e}")
            return

        tmp_dir = Path("/tmp")
        tmp_dir.mkdir(exist_ok=True)
        out_path = tmp_dir / f"{uuid.uuid4().hex}.mp4"

        timeout = aiohttp.ClientTimeout(total=60 * 60)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers or {}, cookies=cookies) as session:
                async with session.get(direct_url, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        logger.error(f"[BOT] Upload upstream error: {resp.status}")
                        await q.edit_message_text(f"Upstream error {resp.status}")
                        return

                    size = resp.headers.get("Content-Length")
                    if size and int(size) > MAX_TG_UPLOAD_MB * 1024 * 1024:
                        await q.edit_message_text(
                            f"Файл слишком большой для отправки ботом (>{MAX_TG_UPLOAD_MB} MB).\n"
                            f"Используйте ссылку: {BASE_URL}/dl/{token}"
                        )
                        return

                    downloaded = 0
                    with out_path.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > MAX_TG_UPLOAD_MB * 1024 * 1024:
                                f.close()
                                out_path.unlink(missing_ok=True)
                                await q.edit_message_text(
                                    f"Файл превысил лимит {MAX_TG_UPLOAD_MB} MB, отправка отменена.\n"
                                    f"Используйте ссылку: {BASE_URL}/dl/{token}"
                                )
                                return
        except Exception as e:
            logger.error(f"[BOT] Download for telegram failed: {e}", exc_info=True)
            await q.edit_message_text(f"Ошибка при скачивании: {e}")
            out_path.unlink(missing_ok=True)
            return

        await q.edit_message_text("Отправляю документ…")
        try:
            with out_path.open("rb") as f:
                await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=f,
                    filename=f"{title}.mp4",
                    caption="Готово.",
                )
            logger.info(f"[BOT] Document sent successfully")
            await q.edit_message_text("Готово: документ отправлен.")
        except Exception as e:
            logger.error(f"[BOT] Telegram send error: {e}", exc_info=True)
            await q.edit_message_text(f"Ошибка отправки в Telegram: {e}")
        finally:
            out_path.unlink(missing_ok=True)


def build_bot_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(on_send, pattern=r"^send\|"))
    return app


bot_app: Optional[Application] = None


@api.on_event("startup")
async def _startup():
    global bot_app
    logger.info("[STARTUP] Starting bot application...")
    bot_app = build_bot_app()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("[STARTUP] Bot is ready")


@api.on_event("shutdown")
async def _shutdown():
    global bot_app
    logger.info("[SHUTDOWN] Stopping bot...")
    if bot_app:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        bot_app = None
    logger.info("[SHUTDOWN] Complete")
