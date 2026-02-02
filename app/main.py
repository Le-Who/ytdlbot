import os
import re
import uuid
import time
import asyncio
import logging
from collections import deque
from typing import Optional, Dict
from urllib.parse import quote, urlparse

from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
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
from .constants import CHUNK_SIZE, SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES, GIF_FORMAT_ID

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip() # Если есть - используем вебхук

LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "30"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "0").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL: BASE_URL = "http://localhost:8000"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")

def safe_remove(path: str) -> None:
    """Удаляет файл, игнорируя ошибки если файл не найден"""
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

def rename_if_exists(src: str, dst: str) -> None:
    """Переименовывает файл если он существует"""
    if src and os.path.exists(src):
        os.rename(src, dst)

# --- ИНИЦИАЛИЗАЦИЯ ---
api = FastAPI()
ytdlp = YtDlpService()
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Кэши (оптимизировано для free tier - меньше памяти)
link_cache: TTLCache = TTLCache(maxsize=500, ttl=LINK_TTL_MINUTES * 60)  # Уменьшено для экономии памяти
info_cache: TTLCache = TTLCache(maxsize=200, ttl=600)  # Кэш форматов на 10 минут (уменьшено)

# Rate Limiter (простой in-memory)
user_rates: TTLCache = TTLCache(maxsize=500, ttl=60)  # Сброс каждую минуту (уменьшено)

URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
PROGRESS_RE = re.compile(r"(\d+\.\d+)%")

def is_supported_url(text: str) -> bool:
    if not URL_RE.search(text or ""): return False

    try:
        parsed = urlparse(text)
        domain = parsed.hostname
        if not domain:
            return False

        if domain in SUPPORTED_PLATFORMS:
            return True
        if domain.endswith(SUPPORTED_PLATFORMS_SUFFIXES):
            return True
        return False
    except Exception:
        return False

def check_rate_limit(user_id: int, limit: int = 5) -> bool:
    """Проверяет лимит запросов пользователя в минуту"""
    current = user_rates.get(user_id, 0)
    if current >= limit:
        return False
    user_rates[user_id] = current + 1
    return True

# --- API ENDPOINTS ---

@api.get("/health")
async def health(): return {"ok": True}

@api.get("/favicon.ico")
async def favicon(): raise HTTPException(404)

@api.get("/dl/{token}")
async def download(token: str):
    logger.info(f"[DOWNLOAD] Token: {token}")
    payload = link_cache.get(token)
    if not payload: raise HTTPException(404, "Link expired")
    
    encoded_filename = quote(payload.get("title") or "video")
    is_gif = payload["format_id"] == GIF_FORMAT_ID
    file_ext = "gif" if is_gif else "mp4"

    async def stream_video_subprocess():
        if is_gif:
            # Для GIF нужно сначала скачать видео, затем конвертировать в GIF
            tmp_dir = os.getenv("TMPDIR", "/tmp")
            video_tmp = os.path.join(tmp_dir, f"ytdl_video_{uuid.uuid4().hex}.mp4")
            
            try:
                # Скачиваем видео
                cmd = ytdlp.build_command(
                    payload["page_url"], payload["format_id"], payload.get("height"),
                    output=video_tmp
                )
                logger.info(f"[STREAM-GIF] Download: {' '.join(cmd)}")
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await proc.wait()
                
                if proc.returncode != 0:
                    err = await proc.stderr.read()
                    error_text = err.decode(errors='ignore')[:500]
                    logger.error(f"[STREAM-GIF] Download error: {error_text}")
                    return
                
                # Конвертируем в GIF
                logger.info(f"[STREAM-GIF] Converting to GIF...")
                ffmpeg_cmd = [
                    "ffmpeg", "-i", video_tmp, "-vf", "fps=10,scale=320:-1:flags=lanczos",
                    "-t", "10", "-y", "-pix_fmt", "rgb24", "-f", "gif", "-"
                ]
                ffmpeg_proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                
                # Consume stderr asynchronously to avoid deadlock
                stderr_data = deque(maxlen=50)
                async def consume_stderr():
                    while True:
                        line = await ffmpeg_proc.stderr.readline()
                        if not line: break
                        stderr_data.append(line)

                stderr_task = asyncio.create_task(consume_stderr())
                
                try:
                    # Stream directly from ffmpeg stdout
                    while True:
                        chunk = await ffmpeg_proc.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    if ffmpeg_proc.returncode is None:
                        try:
                            ffmpeg_proc.kill()
                        except:
                            pass
                    await ffmpeg_proc.wait()
                    await stderr_task

                if ffmpeg_proc.returncode != 0:
                    error_text = b"".join(stderr_data).decode(errors='ignore')[-500:]
                    logger.error(f"[STREAM-GIF] FFmpeg error: {error_text}")
                    return
                
            except Exception as e:
                logger.error(f"[STREAM-GIF] Exception: {e}", exc_info=True)
            finally:
                # Очистка временных файлов
                await asyncio.to_thread(safe_remove, video_tmp)
        else:
            # Обычное видео - стримим напрямую
            cmd = ytdlp.build_command(
                payload["page_url"], payload["format_id"], payload.get("height"),
                output="-"
            )
            logger.info(f"[STREAM] {' '.join(cmd)}")
            
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                
                # Consume stderr asynchronously to avoid deadlock
                stderr_data = deque(maxlen=50)
                async def consume_stderr():
                    while True:
                        line = await proc.stderr.readline()
                        if not line: break
                        stderr_data.append(line)

                stderr_task = asyncio.create_task(consume_stderr())

                # Таймаут для всего стрима (15 минут для free tier)
                stream_timeout = 900
                start_time = time.time()
                
                try:
                    while True:
                        # Проверяем общий таймаут
                        if time.time() - start_time > stream_timeout:
                            logger.error("[STREAM] Overall timeout exceeded")
                            break

                        try:
                            chunk = await asyncio.wait_for(proc.stdout.read(CHUNK_SIZE), timeout=45.0)
                            if not chunk: break
                            yield chunk
                        except asyncio.TimeoutError:
                            logger.warning("[STREAM] Chunk read timeout, continuing...")
                            # Продолжаем попытки, но проверяем общий таймаут
                            continue
                finally:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except:
                            pass
                    await proc.wait()
                    await stderr_task

                if proc.returncode != 0:
                    error_text = b"".join(stderr_data).decode(errors='ignore')[-500:]
                    logger.error(f"[STREAM] Error (code {proc.returncode}): {error_text}")
                    
            except Exception as e:
                logger.error(f"[STREAM] Exception: {e}", exc_info=True)
            finally:
                if proc and proc.returncode is None:
                    try: 
                        proc.kill()
                        await proc.wait()
                    except: pass

    media_type = "image/gif" if is_gif else "application/octet-stream"
    return StreamingResponse(
        stream_video_subprocess(),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"}
    )

# --- WEBHOOK ENDPOINT ---
if WEBHOOK_URL:
    @api.post("/webhook")
    async def telegram_webhook(request: Request):
        """Обработка вебхука от Telegram"""
        if bot_app:
            try:
                update = Update.de_json(await request.json(), bot_app.bot)
                await bot_app.process_update(update)
            except Exception as e:
                logger.error(f"Webhook update error: {e}")
        return {"ok": True}

# --- TELEGRAM HANDLERS ---

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

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()
    
    # Попытка извлечь URL из текста (например, если отправлен "Check this https://...")
    match = URL_RE.search(text)
    if match:
        text = match.group(0).rstrip(".,!:;)")

    if not is_supported_url(text):
        await update.message.reply_text("❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest.")
        return

    # Rate Limit: 10 запросов в минуту
    if not check_rate_limit(user.id, limit=10):
        await update.message.reply_text("⚠️ Слишком часто. Подождите минуту.")
        return

    msg = await update.message.reply_text("🔎 Ищу видео...")

    # 1. Проверяем КЭШ
    cached = info_cache.get(text)
    if cached:
        logger.info(f"[CACHE] Hit: {text}")
        title, formats, audio, duration = cached
    else:
        try:
            # Выполняем парсинг
            title, formats, audio, duration = await asyncio.to_thread(ytdlp.list_formats, text)
            info_cache[text] = (title, formats, audio, duration) # Сохраняем в кэш
        except Exception as e:
            logger.error(f"Parse error: {e}", exc_info=True)
            error_msg = str(e)
            # Более понятные сообщения для пользователя
            if "403" in error_msg or "forbidden" in error_msg.lower():
                await msg.edit_text("❌ Доступ запрещен. Контент может быть приватным или требуется авторизация.")
            elif "404" in error_msg or "not found" in error_msg.lower():
                await msg.edit_text("❌ Видео не найдено. Проверьте правильность ссылки.")
            elif "pinterest" in error_msg.lower() or "pin.it" in error_msg.lower():
                await msg.edit_text("❌ Ошибка загрузки с Pinterest. Попробуйте позже или используйте прямую ссылку на видео.")
            else:
                await msg.edit_text(f"❌ Ошибка: {error_msg[:150]}")
            return

    # Сохраняем контекст
    format_map = {f.format_id: f.height for f in formats}
    format_map[audio.format_id] = None
    
    context.user_data.update({
        "page_url": text, "title": title, "format_map": format_map
    })

    # Клавиатура
    buttons = [[InlineKeyboardButton(f.label, callback_data=f"pick|{f.format_id}")] for f in formats[:6]]
    buttons.append([InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")])

    await msg.edit_text(
        f"📹 <b>{title}</b>\n⏱ {duration}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )

async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    try: _, format_id = q.data.split("|", 1)
    except: return

    data = context.user_data
    if not data.get("page_url"):
        await q.edit_message_text("⚠️ Данные устарели. Пожалуйста, отправьте ссылку на видео еще раз.")
        return

    token = uuid.uuid4().hex
    link_cache[token] = {
        "page_url": data["page_url"],
        "format_id": format_id,
        "height": data["format_map"].get(format_id),
        "title": data["title"]
    }
    
    dl_link = f"{BASE_URL}/dl/{token}"
    
    kb = [[InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append([InlineKeyboardButton("📤 Отправить файл в TG", callback_data=f"send|{token}")])

    await q.edit_message_text(
        f"✅ Ссылка готова ({LINK_TTL_MINUTES} мин):\n\n{dl_link}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True
    )

async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    # Rate Limit для скачивания: 3 раза в минуту
    if not check_rate_limit(user_id, limit=3):
        await q.edit_message_text("⚠️ Слишком часто скачиваете. Подождите.")
        return

    try: _, token = q.data.split("|", 1)
    except: return

    payload = link_cache.get(token)
    if not payload:
        await q.edit_message_text("⚠️ Ссылка устарела.")
        return

    if tasks_sem.locked():
        await q.edit_message_text("⚠️ Очередь переполнена. Скачайте по ссылке.")
        return

    async with tasks_sem:
        await q.edit_message_text("⏳ Начинаю загрузку...")
        # Используем временную директорию с очисткой
        tmp_dir = os.getenv("TMPDIR", "/tmp")
        is_gif = payload["format_id"] == GIF_FORMAT_ID
        file_ext = "gif" if is_gif else "mp4"
        tmp_path = os.path.join(tmp_dir, f"ytdl_{uuid.uuid4().hex}.{file_ext}")

        # Строим команду с Aria2c и MaxFilesize
        cmd = ytdlp.build_command(
            payload["page_url"], payload["format_id"], payload.get("height"),
            output=tmp_path,
            max_filesize=50, use_aria2=True # Используем aria2c для скорости!
        )

        try:
            logger.info(f"[DL-TG] {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT # Объединяем stdout/stderr для парсинга
            )
            
            # --- ЧТЕНИЕ ПРОГРЕССА ---
            last_update = 0
            download_start = time.time()
            max_download_time = 600  # 10 минут максимум для free tier
            
            while True:
                # Проверяем общий таймаут загрузки
                if time.time() - download_start > max_download_time:
                    logger.warning("[DL-TG] Download timeout exceeded")
                    break
                    
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=300.0)
                except asyncio.TimeoutError:
                    logger.warning("[DL-TG] Read timeout, checking process...")
                    # Проверяем, не завершился ли процесс
                    if proc.returncode is not None:
                        break
                    continue
                
                if not line: break
                
                line_str = line.decode('utf-8', errors='ignore').strip()
                
                # Парсим процент "[download]  45.0% of..."
                if "[download]" in line_str and "%" in line_str:
                    now = time.time()
                    if now - last_update > 3.0:  # Обновляем раз в 3 сек
                        match = PROGRESS_RE.search(line_str)
                        if match:
                            try:
                                await q.edit_message_text(f"⏳ Скачиваю: {match.group(1)}%")
                                last_update = now
                            except Exception: 
                                pass  # Игнорим ошибки редактирования (flood wait)
            
            await proc.wait()

            # Если это GIF формат, конвертируем видео в GIF через ffmpeg
            if is_gif and proc.returncode == 0:
                # Сначала скачиваем видео во временный файл
                video_tmp = tmp_path.replace(".gif", "_video.mp4")
                await asyncio.to_thread(rename_if_exists, tmp_path, video_tmp)
                
                # Конвертируем в GIF через ffmpeg
                await q.edit_message_text("⏳ Конвертирую в GIF...")
                ffmpeg_cmd = [
                    "ffmpeg", "-i", video_tmp, "-vf", "fps=10,scale=320:-1:flags=lanczos",
                    "-t", "10", "-y", "-pix_fmt", "rgb24", "-f", "gif", tmp_path
                ]
                logger.info(f"[GIF] {' '.join(ffmpeg_cmd)}")
                ffmpeg_proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await ffmpeg_proc.wait()
                
                # Удаляем временный видео файл
                await asyncio.to_thread(safe_remove, video_tmp)
                
                if ffmpeg_proc.returncode != 0:
                    error_text = (await ffmpeg_proc.stderr.read()).decode('utf-8', errors='ignore')[:200]
                    logger.error(f"[GIF] FFmpeg error: {error_text}")
                    await q.edit_message_text("⚠️ Ошибка конвертации в GIF. Используйте ссылку для скачивания.")
                    await asyncio.to_thread(safe_remove, tmp_path)
                    return

            if proc.returncode != 0:
                # Читаем ошибку для более информативного сообщения
                stderr_data = await proc.stderr.read() if proc.stderr else b""
                error_text = stderr_data.decode('utf-8', errors='ignore').lower()[:200]
                logger.error(f"[DL-TG] Process failed with code {proc.returncode}: {error_text}")
                
                # Более понятные сообщения об ошибках
                if "file larger" in error_text or "filesize" in error_text:
                    await q.edit_message_text("⚠️ Файл слишком большой (>50 МБ). Используйте ссылку для скачивания.")
                elif "403" in error_text or "forbidden" in error_text:
                    await q.edit_message_text("⚠️ Доступ запрещен. Попробуйте скачать по ссылке.")
                else:
                    await q.edit_message_text("⚠️ Ошибка загрузки. Используйте ссылку для скачивания.")
                
                await asyncio.to_thread(safe_remove, tmp_path)
                return

            # Проверяем размер файла
            try:
                file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                if file_size > 49.5 * 1024 * 1024:
                    await asyncio.to_thread(safe_remove, tmp_path)
                    await q.edit_message_text("⚠️ Файл > 50 МБ. Используйте ссылку.")
                    return
            except OSError as e:
                logger.error(f"[DL-TG] Error checking file size: {e}")
                await q.edit_message_text("⚠️ Ошибка проверки файла. Используйте ссылку.")
                await asyncio.to_thread(safe_remove, tmp_path)
                return

            await q.edit_message_text("📤 Загружаю в Telegram...")
            
            # Очищаем имя файла от недопустимых символов для Telegram
            safe_title = SAFE_FILENAME_RE.sub('_', payload.get('title', 'video')[:100])
            
            try:
                with open(tmp_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=q.message.chat_id, 
                        document=f,
                        filename=f"{safe_title}.{file_ext}",
                        caption="✅ Готово!",
                        read_timeout=90,  # Увеличено для медленных соединений
                        write_timeout=90,
                        connect_timeout=30
                    )
                await q.edit_message_text("✅ Отправлено.")
            except NetworkError as net_err:
                # Более детальная обработка сетевых ошибок
                error_str = str(net_err).lower()
                if "413" in error_str or "request entity too large" in error_str:
                    await q.edit_message_text("⚠️ Файл слишком большой для Telegram (>50 МБ). Скачайте по ссылке.")
                elif "timeout" in error_str:
                    await q.edit_message_text("⚠️ Таймаут загрузки. Попробуйте скачать по ссылке.")
                else:
                    await q.edit_message_text("❌ Ошибка отправки в Telegram. Используйте ссылку.")
                logger.error(f"[DL-TG] Network error: {net_err}")

        except NetworkError as e:
            error_str = str(e).lower()
            if "413" in error_str or "request entity too large" in error_str:
                await q.edit_message_text("⚠️ Файл > 50 MB. Скачайте по ссылке.")
            elif "timeout" in error_str:
                await q.edit_message_text("⚠️ Таймаут сети. Попробуйте скачать по ссылке.")
            else:
                await q.edit_message_text("❌ Ошибка сети Telegram. Используйте ссылку.")
            logger.error(f"[DL-TG] Network error: {e}")
        except Exception as e:
            logger.error(f"[DL-TG] Upload error: {e}", exc_info=True)
            await q.edit_message_text("❌ Ошибка загрузки. Используйте ссылку для скачивания.")
        finally:
            # Гарантированная очистка временного файла
            if tmp_path:
                await asyncio.to_thread(safe_remove, tmp_path)

# --- APP BUILDER ---
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
    bot_app = build_bot_app()
    await bot_app.initialize()
    await bot_app.start()
    
    if WEBHOOK_URL:
        # Режим Webhook
        await bot_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        # Режим Polling
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling started")

@api.on_event("shutdown")
async def _shutdown():
    global bot_app
    if bot_app:
        if WEBHOOK_URL: await bot_app.bot.delete_webhook()
        elif bot_app.updater.running: await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
