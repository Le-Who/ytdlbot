import os
import re
import uuid
import asyncio
import logging
from typing import Optional
from urllib.parse import quote

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
from telegram.error import NetworkError

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

api = FastAPI()
ytdlp = YtDlpService()
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

link_cache: TTLCache = TTLCache(maxsize=2000, ttl=LINK_TTL_MINUTES * 60)

URL_RE = re.compile(r"^https?://", re.I)
CHUNK_SIZE = 64 * 1024


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
        or ("tiktok.com" in t)
    )


def build_yt_dlp_command(
    page_url: str,
    format_id: str,
    height: Optional[int],
    output: str,
    cookies_path: Optional[str] = None,
) -> list:
    """
    Строит команду yt-dlp для стриминга.
    
    СТРАТЕГИЯ:
    1. Видео выбираем жестко по высоте: bestvideo[height=X]
    2. Аудио выбираем с приоритетом оригинальной дорожки через format_note
    """
    
    # Селектор видео (по высоте)
    if height:
        video_sel = f"bestvideo[height={height}]"
        prog_sel = f"best[height={height}]"
    elif "+" not in format_id and format_id not in ("bestaudio/best", "best"):
         video_sel = format_id
         prog_sel = f"best"
    else:
        # Сложный формат от пользователя
        cmd = [
            "yt-dlp", "--format", format_id, "--output", output,
            "--quiet", "--no-warnings", "--no-playlist", "--force-ipv4"
        ]
        if cookies_path: 
            cmd.extend(["--cookies", cookies_path])
        cmd.append(page_url)
        return cmd

    # Селектор аудио (Original -> English -> OrigTag -> Any)
    audio_sel = (
        "bestaudio[format_note*=original]/"
        "bestaudio[language^=en]/"
        "bestaudio[language^=orig]/"
        "bestaudio"
    )
    
    final_fmt = f"{video_sel}+({audio_sel})/{prog_sel}/best"

    cmd = [
        "yt-dlp",
        "--format", final_fmt,
        "--output", output,
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        "--force-ipv4",
        # Для стриминга MP4 в pipe
        "--postprocessor-args", "Merger+ffmpeg:-movflags frag_keyframe+empty_moov"
    ]
    
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    
    cmd.append(page_url)
    
    return cmd


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/favicon.ico")
async def favicon():
    raise HTTPException(status_code=404, detail="No favicon")


@api.get("/dl/{token}")
async def download(token: str):
    logger.info(f"[DOWNLOAD] Token request: {token}")

    payload = link_cache.get(token)
    if not payload:
        logger.warning(f"[DOWNLOAD] Token expired or invalid: {token}")
        raise HTTPException(status_code=404, detail="Link expired or not found")

    page_url = payload["page_url"]
    format_id = payload["format_id"]
    height = payload.get("height")
    title = payload.get("title") or "video"

    encoded_filename = quote(title)
    
    logger.info(f"[DOWNLOAD] URL: {page_url}")
    logger.info(f"[DOWNLOAD] Height: {height} (Format: {format_id})")

    async def stream_video_subprocess():
        cmd = build_yt_dlp_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output="-", 
            cookies_path=ytdlp.cookies_path,
        )
        
        logger.info(f"[DOWNLOAD] CMD: {' '.join(cmd)}")

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            while True:
                chunk = await proc.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

            await proc.wait()

            if proc.returncode != 0:
                err_bytes = await proc.stderr.read()
                err_text = err_bytes.decode(errors="ignore")
                logger.error(f"[DOWNLOAD] yt-dlp error: {err_text}")

        except Exception as e:
            logger.error(f"[DOWNLOAD] Streaming error: {e}", exc_info=True)
            if proc:
                try: 
                    proc.kill()
                except: 
                    pass

    return StreamingResponse(
        stream_video_subprocess(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.mp4"
        }
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Отправьте ссылку на видео (YouTube / TikTok / VK / RuTube).\n"
        "Бот предложит качество и создаст ссылку для скачивания."
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not is_supported_url(text):
        await update.message.reply_text(
            "❌ Пришлите ссылку на YouTube, TikTok, VK или RuTube."
        )
        return

    msg = await update.message.reply_text("⏳ Анализирую видео...")
    try:
        logger.info(f"[BOT] Analyzing: {text}")
        title, formats, audio, duration = await asyncio.to_thread(
            ytdlp.list_formats, text
        )
        logger.info(f"[BOT] Success: {title}")
    except Exception as e:
        logger.error(f"[BOT] Parse error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Ошибка парсинга: {str(e)[:120]}")
        return

    format_map = {f.format_id: f.height for f in formats}
    format_map[audio.format_id] = None
    
    context.user_data["page_url"] = text
    context.user_data["title"] = title
    context.user_data["format_map"] = format_map

    buttons = []
    for f in formats[:6]:
        buttons.append(
            [InlineKeyboardButton(f.label, callback_data=f"pick|{f.format_id}")]
        )

    buttons.append(
        [InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")]
    )

    await msg.edit_text(
        f"📹 <b>{title}</b>\n⏱ {duration}\n\nВыберите качество:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        _, format_id = q.data.split("|", 1)
    except Exception:
        await q.edit_message_text("Ошибка выбора.")
        return

    page_url = context.user_data.get("page_url")
    title = context.user_data.get("title", "video")
    
    format_map = context.user_data.get("format_map", {})
    height = format_map.get(format_id)

    if not page_url:
        await q.edit_message_text("⚠️ Сессия устарела. Отправьте ссылку заново.")
        return

    token = uuid.uuid4().hex
    link_cache[token] = {
        "page_url": page_url, 
        "format_id": format_id, 
        "height": height, 
        "title": title
    }
    
    dl_link = f"{BASE_URL}/dl/{token}"

    logger.info(f"[BOT] Link generated: {dl_link} (ID={format_id}, H={height})")

    kb = [[InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [
                InlineKeyboardButton(
                    "📤 Отправить файл в TG", callback_data=f"send|{token}"
                )
            ]
        )

    await q.edit_message_text(
        f"✅ Ссылка готова (живет {LINK_TTL_MINUTES} мин):\n\n{dl_link}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
    )


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Для отправки в Telegram нужно сохранить файл на диск (временно).
    Ограничение Telegram Bot API: 50 МБ максимум.
    """
    q = update.callback_query
    await q.answer()

    try:
        _, token = q.data.split("|", 1)
    except Exception:
        await q.edit_message_text("Ошибка.")
        return

    payload = link_cache.get(token)
    if not payload:
        await q.edit_message_text("⚠️ Ссылка устарела.")
        return

    page_url = payload["page_url"]
    format_id = payload["format_id"]
    height = payload.get("height")
    title = payload.get("title") or "video"

    logger.info(f"[BOT] TG send requested. ID={format_id}, H={height}")

    async with tasks_sem:
        await q.edit_message_text("⏳ Скачиваю файл на сервер...")

        tmp_path = f"/tmp/{uuid.uuid4().hex}.mp4"

        cmd = build_yt_dlp_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=tmp_path,
            cookies_path=ytdlp.cookies_path,
        )

        try:
            logger.info(f"[BOT] Downloading to file: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"[BOT] Download failed: {stderr.decode(errors='ignore')}")
                await q.edit_message_text("❌ Ошибка при скачивании файла.")
                return

            size = os.path.getsize(tmp_path)
            
            # Строгая проверка: лимит Telegram 50 МБ, оставляем запас
            size_mb = size / (1024 * 1024)
            if size > 49 * 1024 * 1024:
                os.unlink(tmp_path)
                await q.edit_message_text(
                    f"⚠️ Файл ({size_mb:.1f} MB) превышает лимит Telegram Bot API (50 MB).\n\n"
                    f"📥 Пожалуйста, скачайте его по прямой ссылке выше 👆"
                )
                return

            await q.edit_message_text("📤 Отправляю в Telegram...")

            with open(tmp_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=f,
                    filename=f"{title}.mp4",
                    caption="✅ Готово!",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60,
                )

            await q.edit_message_text("✅ Документ отправлен.")

        except NetworkError as e:
            # Обработка ошибки 413 (файл слишком большой для Telegram API)
            if "Request Entity Too Large" in str(e) or "413" in str(e):
                logger.warning(f"[BOT] File too large for TG API: {e}")
                await q.edit_message_text(
                    "⚠️ Файл оказался слишком большим для серверов Telegram (>50 MB).\n\n"
                    "📥 Используйте прямую ссылку для скачивания."
                )
            else:
                logger.error(f"[BOT] Network error: {e}", exc_info=True)
                await q.edit_message_text(f"❌ Ошибка сети при отправке в Telegram.")

        except Exception as e:
            logger.error(f"[BOT] Upload error: {e}", exc_info=True)
            await q.edit_message_text(f"❌ Ошибка отправки: {str(e)[:100]}")
            
        finally:
            # Всегда удаляем временный файл
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception as cleanup_err:
                    logger.error(f"[BOT] Cleanup error: {cleanup_err}")


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
    logger.info("[STARTUP] Initializing bot...")
    bot_app = build_bot_app()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    logger.info("[STARTUP] Bot polling started")


@api.on_event("shutdown")
async def _shutdown():
    global bot_app
    if bot_app:
        logger.info("[SHUTDOWN] Stopping bot...")
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        bot_app = None
