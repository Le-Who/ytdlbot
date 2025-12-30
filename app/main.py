import os
import re
import uuid
import asyncio
import logging
import html
from pathlib import Path
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

from .ytdlp_service import YtDlpService
from .constants import (
    CHUNK_SIZE,
    MAX_FORMATS_DISPLAY,
    DOWNLOAD_TIMEOUT_SECONDS,
    UPLOAD_TIMEOUT_SECONDS,
    SUPPORTED_PLATFORMS,
)

load_dotenv()

# Настройки из окружения
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

# Инициализация FastAPI и глобальных объектов
api = FastAPI()
ytdlp = YtDlpService()
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
link_cache: TTLCache = TTLCache(maxsize=2000, ttl=LINK_TTL_MINUTES * 60)

URL_RE = re.compile(r"^https?://", re.I)


def is_supported_url(text: str) -> bool:
    """Проверяет, поддерживается ли URL"""
    if not URL_RE.search(text or ""):
        return False
    
    t = text.lower()
    return any(platform in t for platform in SUPPORTED_PLATFORMS)


def build_complex_format(format_id: str) -> str:
    """
    Создаёт комплексный формат с фильтрацией аудио по языку
    
    Пример: '137+bestaudio[vcodec=none][language^=en]/...'
    """
    if "+" in format_id or format_id in ["bestaudio/best", "best"]:
        return format_id
    
    return (
        f"{format_id}+bestaudio[vcodec=none][language^=en]/"
        f"{format_id}+bestaudio[vcodec=none][language^=orig]/"
        f"{format_id}+bestaudio[vcodec=none]/best"
    )


@api.get("/health")
async def health():
    """Health check endpoint"""
    return {"ok": True}


@api.get("/favicon.ico")
async def favicon():
    """Заглушка для favicon"""
    raise HTTPException(status_code=404, detail="No favicon")


@api.get("/dl/{token}")
async def download(token: str):
    """Скачивание видео по токену"""
    logger.info(f"[DOWNLOAD] Token request: {token}")
    
    payload = link_cache.get(token)
    if not payload:
        logger.warning(f"[DOWNLOAD] Token expired or invalid: {token}")
        raise HTTPException(status_code=404, detail="Link expired or not found")
    
    page_url = payload["page_url"]
    format_id = payload["format_id"]
    title = payload.get("title") or "video"
    
    encoded_filename = quote(title)
    logger.info(f"[DOWNLOAD] Starting stream for: {page_url} (Format: {format_id})")
    
    async def stream_video_subprocess():
        """Стримит видео через subprocess yt-dlp"""
        complex_format = build_complex_format(format_id)
        
        cmd = [
            "yt-dlp",
            "--format", complex_format,
            "--output", "-",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--force-ipv4",
        ]
        
        if ytdlp.cookies_path:
            cmd.extend(["--cookies", ytdlp.cookies_path])
        
        cmd.append(page_url)
        
        logger.info(f"[DOWNLOAD] Executing CMD: {' '.join(cmd)}")
        
        proc = None
        try:
            # Запускаем subprocess с таймаутом
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                ),
                timeout=10  # Таймаут на запуск
            )
            
            # Стримим данные с таймаутом на всё скачивание
            async def stream_with_timeout():
                while True:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(CHUNK_SIZE),
                        timeout=60  # 60 сек на чанк
                    )
                    if not chunk:
                        break
                    yield chunk
                
                # Ждём завершения процесса
                await asyncio.wait_for(proc.wait(), timeout=30)
                
                if proc.returncode != 0:
                    stderr_output = await proc.stderr.read()
                    err_text = stderr_output.decode(errors='ignore')
                    logger.error(f"[DOWNLOAD] yt-dlp error: {err_text}")
            
            async for chunk in stream_with_timeout():
                yield chunk
                
        except asyncio.TimeoutError:
            logger.error(f"[DOWNLOAD] Timeout exceeded for {page_url}")
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[DOWNLOAD] Streaming exception: {e}", exc_info=True)
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
    
    return StreamingResponse(
        stream_video_subprocess(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.mp4"
        }
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "👋 Отправьте ссылку на видео (YouTube / TikTok / VK / RuTube).\n"
        "Бот предложит качество и создаст ссылку для скачивания."
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений с URL"""
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
        await msg.edit_text(f"❌ Ошибка парсинга: {str(e)[:100]}")
        return
    
    # Сохраняем данные в контексте
    context.user_data["page_url"] = text
    context.user_data["title"] = title
    
    # Создаём кнопки
    buttons = []
    for f in formats[:MAX_FORMATS_DISPLAY]:
        buttons.append([
            InlineKeyboardButton(f.label, callback_data=f"pick|{f.format_id}")
        ])
    
    buttons.append([
        InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")
    ])
    
    # Экранируем HTML-символы в названии
    safe_title = html.escape(title)
    
    await msg.edit_text(
        f"📹 <b>{safe_title}</b>\n⏱ {duration}\n\nВыберите качество:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора качества"""
    q = update.callback_query
    await q.answer()
    
    try:
        _, format_id = q.data.split("|", 1)
    except ValueError:
        await q.edit_message_text("❌ Ошибка выбора формата.")
        return
    
    page_url = context.user_data.get("page_url")
    title = context.user_data.get("title", "video")
    
    if not page_url:
        await q.edit_message_text("⚠️ Сессия устарела. Отправьте ссылку заново.")
        return
    
    # Генерируем токен и ссылку
    token = uuid.uuid4().hex
    link_cache[token] = {
        "page_url": page_url,
        "format_id": format_id,
        "title": title
    }
    dl_link = f"{BASE_URL}/dl/{token}"
    
    logger.info(f"[BOT] Link generated: {dl_link}")
    
    # Создаём кнопки
    kb = [[InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append([
            InlineKeyboardButton("📤 Отправить файл в TG", callback_data=f"send|{token}")
        ])
    
    await q.edit_message_text(
        f"✅ Ссылка готова (живет {LINK_TTL_MINUTES} мин):\n\n{dl_link}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
    )


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик отправки файла в Telegram"""
    q = update.callback_query
    await q.answer()
    
    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        await q.edit_message_text("❌ Ошибка.")
        return
    
    payload = link_cache.get(token)
    if not payload:
        await q.edit_message_text("⚠️ Ссылка устарела.")
        return
    
    page_url = payload["page_url"]
    format_id = payload["format_id"]
    title = payload.get("title") or "video"
    
    async with tasks_sem:
        await q.edit_message_text("⏳ Скачиваю файл на сервер...")
        
        tmp_dir = Path("/tmp")
        tmp_dir.mkdir(exist_ok=True)
        out_path = tmp_dir / f"{uuid.uuid4().hex}.mp4"
        
        complex_format = build_complex_format(format_id)
        
        cmd = [
            "yt-dlp",
            "--format", complex_format,
            "--output", str(out_path),
            "--quiet", "--no-warnings", "--no-playlist",
            "--force-ipv4",
        ]
        
        if ytdlp.cookies_path:
            cmd.extend(["--cookies", ytdlp.cookies_path])
        
        cmd.append(page_url)
        
        try:
            logger.info(f"[BOT] Downloading to file: {' '.join(cmd)}")
            
            # Скачиваем с таймаутом
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                ),
                timeout=DOWNLOAD_TIMEOUT_SECONDS
            )
            
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                logger.error(f"[BOT] Download failed: {stderr.decode()}")
                await q.edit_message_text("❌ Ошибка при скачивании файла.")
                return
            
            # Проверяем размер файла
            size = out_path.stat().st_size
            if size > MAX_TG_UPLOAD_MB * 1024 * 1024:
                out_path.unlink(missing_ok=True)
                await q.edit_message_text(
                    f"⚠️ Файл слишком большой ({int(size/1024/1024)} MB). "
                    "Используйте ссылку."
                )
                return
            
            await q.edit_message_text("📤 Отправляю в Telegram...")
            
            # Отправляем файл с таймаутом
            with out_path.open("rb") as f:
                await asyncio.wait_for(
                    context.bot.send_document(
                        chat_id=q.message.chat_id,
                        document=f,
                        filename=f"{title}.mp4",
                        caption="✅ Готово!",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60
                    ),
                    timeout=UPLOAD_TIMEOUT_SECONDS
                )
            
            await q.edit_message_text("✅ Документ отправлен.")
            
        except asyncio.TimeoutError:
            logger.error(f"[BOT] Timeout during download/upload")
            await q.edit_message_text("❌ Превышен таймаут. Попробуйте позже.")
        except Exception as e:
            logger.error(f"[BOT] Upload error: {e}", exc_info=True)
            await q.edit_message_text(f"❌ Ошибка: {str(e)[:100]}")
        finally:
            # Всегда удаляем временный файл
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete temp file: {e}")


def build_bot_app() -> Application:
    """Создаёт и настраивает Telegram bot application"""
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(on_send, pattern=r"^send\|"))
    
    return app


bot_app: Optional[Application] = None


@api.on_event("startup")
async def _startup():
    """Инициализация при запуске приложения"""
    global bot_app
    logger.info("[STARTUP] Initializing bot...")
    
    bot_app = build_bot_app()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("[STARTUP] Bot polling started")


@api.on_event("shutdown")
async def _shutdown():
    """Очистка при завершении приложения"""
    global bot_app
    
    if bot_app:
        logger.info("[SHUTDOWN] Stopping bot...")
        
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        
        bot_app = None
    
    # Очищаем cookies
    if ytdlp.cookies_manager:
        ytdlp.cookies_manager.cleanup()
    
    logger.info("[SHUTDOWN] Cleanup complete")
