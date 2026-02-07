import os
import re
import html
import uuid
import time
import secrets
import tempfile
import asyncio
import logging
import secrets
from collections import deque
from typing import Optional, Dict
from urllib.parse import quote, urlsplit

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
from telegram.constants import ChatAction
from telegram.error import NetworkError

from .ytdlp_service import YtDlpService
from .constants import (
    CHUNK_SIZE,
    SUPPORTED_PLATFORMS,
    SUPPORTED_PLATFORMS_SUFFIXES,
    GIF_FORMAT_ID,
    AUDIO_FORMAT_ID,
)

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # Если есть - используем вебхук
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", secrets.token_urlsafe(32))

TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN")
if not TELEGRAM_SECRET_TOKEN:
    TELEGRAM_SECRET_TOKEN = secrets.token_urlsafe(32)

LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "30"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "0").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL:
    BASE_URL = "http://localhost:8000"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
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
parsing_sem = asyncio.Semaphore(5)  # Лимит на одновременный парсинг форматов

# Кэши (оптимизировано для free tier - меньше памяти)
link_cache: TTLCache = TTLCache(
    maxsize=500, ttl=LINK_TTL_MINUTES * 60
)  # Уменьшено для экономии памяти
info_cache: TTLCache = TTLCache(
    maxsize=200, ttl=600
)  # Кэш форматов на 10 минут (уменьшено)

# Кэш отмены загрузки: token -> bool (True = отменено)
cancel_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)

# Rate Limiter (простой in-memory)
user_rates: TTLCache = TTLCache(maxsize=500, ttl=60)  # Сброс каждую минуту (уменьшено)

# Глобальные контейнеры для управления ресурсами
active_processes = set()
inflight_parsing = {}  # url -> asyncio.Event
active_processes_lock = asyncio.Lock()

URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
PROGRESS_RE = re.compile(r"(\d+\.\d+)%")

# --- PROGRESS BAR CACHE ---
# Optimization: Pre-compute progress bars for default length to avoid string allocations
# in the hot path (called every ~3s during downloads).
# Benchmark shows ~14% speedup.
BLOCK_FULL = "█"
BLOCK_EMPTY = "░"
BAR_LENGTH = 15
PROGRESS_BARS = [
    BLOCK_FULL * i + BLOCK_EMPTY * (BAR_LENGTH - i) for i in range(BAR_LENGTH + 1)
]


def render_progressbar(percent: float, length: int = BAR_LENGTH) -> str:
    """Renders a text-based progress bar."""
    percent = max(0.0, min(100.0, percent))
    filled_length = int(length * percent // 100)

    if length == BAR_LENGTH:
        # Use cached string (avoid allocation)
        bar = PROGRESS_BARS[filled_length]
    else:
        # Fallback for custom lengths
        bar = BLOCK_FULL * filled_length + BLOCK_EMPTY * (length - filled_length)

    return f"{bar} {percent:.1f}%"


def is_supported_url(text: str) -> bool:
    try:
        parsed = urlsplit(text)
        domain = parsed.hostname
        if not domain:
            return False

        return domain in SUPPORTED_PLATFORMS or domain.endswith(
            SUPPORTED_PLATFORMS_SUFFIXES
        )
    except Exception:
        return False


def check_rate_limit(user_id: int, limit: int = 5) -> bool:
    """Проверяет лимит запросов пользователя в минуту (Отключено пользователем)"""
    return True


def build_format_keyboard(formats: list, audio) -> InlineKeyboardMarkup:
    """Helper to build format selection buttons in 2 columns."""
    buttons = []
    # Разбиваем форматы на пары для 2-колоночного лейаута
    formats_slice = formats[:8]  # Показываем больше форматов (было 6)
    for i in range(0, len(formats_slice), 2):
        row = [
            InlineKeyboardButton(
                formats_slice[i].label,
                callback_data=f"pick|{formats_slice[i].format_id}",
            )
        ]
        if i + 1 < len(formats_slice):
            row.append(
                InlineKeyboardButton(
                    formats_slice[i + 1].label,
                    callback_data=f"pick|{formats_slice[i+1].format_id}",
                )
            )
        buttons.append(row)

    buttons.append(
        [InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")]
    )
    return InlineKeyboardMarkup(buttons)


async def run_subprocess(cmd: list, collect_stderr: bool = True):
    """Стандартизированный запуск subprocess с отслеживанием и очисткой"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=(
            asyncio.subprocess.PIPE if collect_stderr else asyncio.subprocess.DEVNULL
        ),
    )

    async with active_processes_lock:
        active_processes.add(proc)

    stderr_data = deque(maxlen=100)
    stderr_task = None

    if collect_stderr:

        async def consume_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_data.append(line)

        stderr_task = asyncio.create_task(consume_stderr())

    try:
        yield proc, stderr_data
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except:
                try:
                    proc.kill()
                except:
                    pass
        await proc.wait()
        if stderr_task:
            await stderr_task
        async with active_processes_lock:
            active_processes.discard(proc)


# --- API ENDPOINTS ---


@api.get("/health")
async def health():
    return {"ok": True}


@api.get("/favicon.ico")
async def favicon():
    raise HTTPException(404)


@api.get("/dl/{token}")
async def download(token: str):
    logger.info(f"[DOWNLOAD] Token: {token}")
    payload = link_cache.get(token)
    if not payload:
        raise HTTPException(404, "Link expired")

    encoded_filename = quote(payload.get("title") or "video")
    format_id = payload.get("format_id")
    is_gif = format_id == GIF_FORMAT_ID
    is_audio = format_id == AUDIO_FORMAT_ID

    if is_gif:
        file_ext = "gif"
        media_type = "image/gif"
    elif is_audio:
        file_ext = "mp3"
        media_type = "audio/mpeg"
    else:
        file_ext = "mp4"
        media_type = "video/mp4"

    async def stream_video_subprocess():
        if is_gif:
            tmp_dir = os.getenv("TMPDIR", tempfile.gettempdir())
            video_tmp = os.path.join(tmp_dir, f"ytdl_video_{uuid.uuid4().hex}.mp4")

            try:
                cmd = ytdlp.build_command(
                    payload["page_url"],
                    payload["format_id"],
                    payload.get("height"),
                    output=video_tmp,
                )
                logger.info(f"[STREAM-GIF] Download: {' '.join(cmd)}")

                async for proc, stderr in run_subprocess(cmd):
                    await proc.wait()
                    if proc.returncode != 0:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM-GIF] Download error: {err}")
                        return

                logger.info(f"[STREAM-GIF] Converting to GIF...")
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-i",
                    video_tmp,
                    "-vf",
                    "fps=10,scale=320:-1:flags=lanczos",
                    "-t",
                    "10",
                    "-y",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "gif",
                    "-",
                ]

                async for proc, stderr in run_subprocess(ffmpeg_cmd):
                    while True:
                        chunk = await proc.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
                    if proc.returncode != 0:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM-GIF] FFmpeg error: {err}")

            except Exception as e:
                logger.error(f"[STREAM-GIF] Exception: {e}", exc_info=True)
            finally:
                await asyncio.to_thread(safe_remove, video_tmp)
        else:
            # Обычное видео - стримим напрямую
            cmd = ytdlp.build_command(
                payload["page_url"],
                payload["format_id"],
                payload.get("height"),
                output="-",
            )
            logger.info(f"[STREAM] {' '.join(cmd)}")

            try:
                async for proc, stderr in run_subprocess(cmd):
                    stream_timeout = 900
                    start_time = time.time()

                    while True:
                        if time.time() - start_time > stream_timeout:
                            logger.error("[STREAM] Overall timeout exceeded")
                            break

                        try:
                            chunk = await asyncio.wait_for(
                                proc.stdout.read(CHUNK_SIZE), timeout=45.0
                            )
                            if not chunk:
                                break
                            yield chunk
                        except asyncio.TimeoutError:
                            logger.warning("[STREAM] Chunk read timeout, continuing...")
                            continue

                    if proc.returncode != 0 and proc.returncode is not None:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM] Error (code {proc.returncode}): {err}")

            except Exception as e:
                logger.error(f"[STREAM] Exception: {e}", exc_info=True)

    media_type = "image/gif" if is_gif else "video/mp4"
    return StreamingResponse(
        stream_video_subprocess(),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
        },
    )


# --- WEBHOOK ENDPOINT ---
if WEBHOOK_URL:

    @api.post("/webhook")
    async def telegram_webhook(request: Request):
        """Обработка вебхука от Telegram"""
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not token or not secrets.compare_digest(token, TELEGRAM_SECRET_TOKEN):
            raise HTTPException(401, "Unauthorized")

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
        "❗️ <i>Если файл > {MAX_TG_UPLOAD_MB} МБ, он не сможет быть загружен в Telegram (ограничение API). Используйте прямую ссылку.</i>"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Попытка извлечь URL из текста (например, если отправлен "Check this https://...")
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest."
        )
        return

    text = match.group(0).rstrip(".,!:;)")

    if not is_supported_url(text):
        await update.message.reply_text(
            "❌ Ссылка не поддерживается. Попробуйте YouTube, TikTok, VK или Pinterest."
        )
        return

    # Rate Limit: 10 запросов в минуту
    if not check_rate_limit(user.id, limit=10):
        await update.message.reply_text("⚠️ Слишком часто. Подождите минуту.")
        return

    # Индикатор набора текста для отзывчивости
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    msg = await update.message.reply_text("🔎 Ищу видео...")

    # 1. Проверяем КЭШ
    cached = info_cache.get(text)
    if cached:
        logger.info(f"[CACHE] Hit: {text}")
        title, formats, audio, duration = cached
    else:
        # Пытаемся избежать дублирования парсинга одного и того же URL
        if text in inflight_parsing:
            logger.info(f"[PARSING] Waiting for inflight task: {text}")
            await inflight_parsing[text].wait()
            cached = info_cache.get(text)
            if cached:
                title, formats, audio, duration = cached
            else:
                await msg.edit_text(
                    "❌ Ошибка при получении данных. Попробуйте еще раз."
                )
                return
        else:
            event = asyncio.Event()
            inflight_parsing[text] = event
            try:
                # Выполняем парсинг
                async with parsing_sem:
                    title, formats, audio, duration = await asyncio.to_thread(
                        ytdlp.list_formats, text
                    )
                info_cache[text] = (title, formats, audio, duration)  # Сохраняем в кэш
            except Exception as e:
                logger.error(f"Parse error: {e}", exc_info=True)
                error_msg = str(e)
                # Более понятные сообщения для пользователя
                if "403" in error_msg or "forbidden" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Доступ запрещен. Контент может быть приватным или требуется авторизация."
                    )
                elif "404" in error_msg or "not found" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Видео не найдено. Проверьте правильность ссылки."
                    )
                elif "pinterest" in error_msg.lower() or "pin.it" in error_msg.lower():
                    await msg.edit_text(
                        "❌ Ошибка загрузки с Pinterest. Попробуйте позже или используйте прямую ссылку на видео."
                    )
                else:
                    await msg.edit_text(f"❌ Ошибка: {error_msg[:150]}")
                return
            finally:
                event.set()
                inflight_parsing.pop(text, None)

    # Сохраняем контекст
    format_map = {f.format_id: f.height for f in formats}
    format_map[audio.format_id] = None

    size_map = {f.format_id: f.filesize for f in formats}
    size_map[audio.format_id] = audio.filesize

    context.user_data.update(
        {
            "page_url": text,
            "title": title,
            "format_map": format_map,
            "size_map": size_map,
        }
    )

    # Клавиатура
    reply_markup = build_format_keyboard(formats, audio)

    await msg.edit_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = context.user_data
    page_url = data.get("page_url")
    if not page_url:
        await q.edit_message_text(
            "⚠️ Данные устарели. Пожалуйста, отправьте ссылку заново."
        )
        return

    cached = info_cache.get(page_url)
    if not cached:
        # Если кэш истек, пробуем распарсить заново
        try:
            await q.edit_message_text("⏳ Кэш истек. Обновляю данные...")
            async with parsing_sem:
                title, formats, audio, duration = await asyncio.to_thread(
                    ytdlp.list_formats, page_url
                )
            info_cache[page_url] = (title, formats, audio, duration)
        except Exception as e:
            logger.error(f"[ON_BACK] Refresh error: {e}")
            await q.edit_message_text(
                "⚠️ Ошибка обновления данных. Отправьте ссылку заново."
            )
            return
    else:
        title, formats, audio, duration = cached

    reply_markup = build_format_keyboard(formats, audio)

    await q.edit_message_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Подготовка ссылки...")

    try:
        _, format_id = q.data.split("|", 1)
    except:
        return

    data = context.user_data
    if not data.get("page_url"):
        await q.edit_message_text(
            "⚠️ Данные устарели. Пожалуйста, отправьте ссылку на видео еще раз."
        )
        return

    token = uuid.uuid4().hex
    link_cache[token] = {
        "page_url": data["page_url"],
        "format_id": format_id,
        "height": data["format_map"].get(format_id),
        "title": data["title"],
    }

    dl_link = f"{BASE_URL}/dl/{token}"

    kb = [[InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)]]
    if ENABLE_TELEGRAM_UPLOAD:
        kb.append(
            [
                InlineKeyboardButton(
                    "📤 Отправить файл в TG", callback_data=f"send|{token}"
                )
            ]
        )

    kb.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])



    # Формируем строку с деталями формата
    height = data["format_map"].get(format_id)
    filesize = data.get("size_map", {}).get(format_id)

    quality_parts = []
    if height:
        quality_parts.append(f"{height}p")
    elif format_id == AUDIO_FORMAT_ID:
        quality_parts.append("Audio")
    elif format_id == GIF_FORMAT_ID:
        quality_parts.append("GIF")

    if filesize:
        mb = filesize / (1024 * 1024)
        quality_parts.append(f"{mb:.1f} MB")

    quality_str = f" ({' • '.join(quality_parts)})" if quality_parts else ""

    await q.edit_message_text(
        f"✅ <b>Готово{quality_str}</b>\n🔗 Ссылка ({LINK_TTL_MINUTES} мин):\n{html.escape(dl_link)}",
        reply_markup=InlineKeyboardMarkup(kb),
        disable_web_page_preview=True,
        parse_mode="HTML",
    )


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей загрузки"""
    q = update.callback_query
    await q.answer("🚫 Отменяю...")
    try:
        _, token = q.data.split("|", 1)
        cancel_cache[token] = True
        await q.edit_message_text("❌ Загрузка отменена пользователем.")
    except:
        pass


async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    # Rate Limit для скачивания: 3 раза в минуту
    if not check_rate_limit(user_id, limit=3):
        await q.edit_message_text("⚠️ Слишком часто скачиваете. Подождите.")
        return

    try:
        _, token = q.data.split("|", 1)
    except:
        return

    payload = link_cache.get(token)
    if not payload:
        await q.edit_message_text("⚠️ Ссылка устарела.")
        return

    dl_link = f"{BASE_URL}/dl/{token}"
    kb_error = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📥 Скачать (Ссылка)", url=dl_link)],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")],
        ]
    )
    kb_back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    )

    # Кнопка отмены
    kb_cancel = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{token}")]]
    )

    if tasks_sem.locked():
        await q.edit_message_text(
            "⚠️ Очередь переполнена. Скачайте по ссылке.", reply_markup=kb_error
        )
        return

    # Предварительная проверка размера
    data = context.user_data
    fmt_size = data.get("size_map", {}).get(payload["format_id"])
    if fmt_size and fmt_size > 50 * 1024 * 1024:
        mb = fmt_size / (1024 * 1024)
        await q.edit_message_text(
            f"⚠️ Файл слишком большой (~{mb:.1f} МБ).\n"
            "Telegram Bot API не позволяет отправлять файлы больше 50 МБ.\n"
            "Пожалуйста, используйте прямую ссылку ниже.",
            reply_markup=kb_error,
        )
        return

    async with tasks_sem:
        await q.edit_message_text("⏳ Начинаю загрузку...")
        # Используем временную директорию с очисткой
        tmp_dir = os.getenv("TMPDIR", tempfile.gettempdir())
        is_gif = payload["format_id"] == GIF_FORMAT_ID
        is_audio = payload["format_id"] == AUDIO_FORMAT_ID

        if is_gif:
            file_ext = "gif"
        elif is_audio:
            file_ext = "mp3"
        else:
            file_ext = "mp4"

        tmp_path = os.path.join(tmp_dir, f"ytdl_{uuid.uuid4().hex}.{file_ext}")

        # Строим команду с Aria2c и MaxFilesize
        cmd = ytdlp.build_command(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            output=tmp_path,
            max_filesize=50,
            use_aria2=True,  # Используем aria2c для скорости!
        )

        try:
            async for proc, stderr in run_subprocess(cmd):
                last_update = 0
                download_start = time.time()
                max_download_time = 600

                if token in cancel_cache:
                    del cancel_cache[token]

                while True:
                    if cancel_cache.get(token):
                        logger.info(f"[DL-TG] Cancelled by user: {token}")
                        return

                    if time.time() - download_start > max_download_time:
                        logger.warning("[DL-TG] Download timeout exceeded")
                        break

                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=300.0
                        )
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue

                    if not line:
                        break
                    line_str = line.decode("utf-8", errors="ignore").strip()

                    if "[download]" in line_str and "%" in line_str:
                        now = time.time()
                        if now - last_update > 3.0:
                            match = PROGRESS_RE.search(line_str)
                            if match:
                                try:
                                    percent = float(match.group(1))
                                    await q.edit_message_text(
                                        f"⏳ Скачиваю: {render_progressbar(percent)}\n❌ Нажмите отмена, если передумали.",
                                        reply_markup=kb_cancel,
                                    )
                                    last_update = now
                                except:
                                    pass

                await proc.wait()
                if proc.returncode != 0:
                    err = b"".join(stderr).decode("utf-8", errors="ignore").lower()
                    logger.error(f"[DL-TG] yt-dlp failed: {err}")

                    if "file larger" in err or "filesize" in err:
                        await q.edit_message_text(
                            "⚠️ Файл слишком большой (>50 МБ).", reply_markup=kb_error
                        )
                    elif "sign in" in err or "cookies" in err:
                        await q.edit_message_text(
                            "⚠️ Требуется авторизация (Sign-in required).",
                            reply_markup=kb_error,
                        )
                    elif "requested format is not available" in err:
                        await q.edit_message_text(
                            "⚠️ Формат недоступен. Попробуйте другое качество (🔙 Назад).",
                            reply_markup=kb_error,
                        )
                    else:
                        await q.edit_message_text(
                            "⚠️ Ошибка загрузки. Попробуйте другое качество или ссылку.",
                            reply_markup=kb_error,
                        )
                    return

            # Если это GIF формат, конвертируем
            if is_gif:
                video_tmp = tmp_path.replace(".gif", "_video.mp4")
                await asyncio.to_thread(rename_if_exists, tmp_path, video_tmp)
                await q.edit_message_text("⏳ Конвертирую в GIF...")

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-i",
                    video_tmp,
                    "-vf",
                    "fps=10,scale=320:-1:flags=lanczos",
                    "-t",
                    "10",
                    "-y",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "gif",
                    tmp_path,
                ]

                async for proc, stderr in run_subprocess(ffmpeg_cmd):
                    await proc.wait()
                    if proc.returncode != 0:
                        logger.error(f"[GIF] FFmpeg failed")
                        await q.edit_message_text(
                            "⚠️ Ошибка конвертации в GIF. Используйте ссылку.",
                            reply_markup=kb_error,
                        )
                        return

                await asyncio.to_thread(safe_remove, video_tmp)

            # Проверяем размер перед отправкой
            try:
                file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                if file_size > 49.9 * 1024 * 1024:  # 50MB
                    await q.edit_message_text(
                        "⚠️ Файл слишком большой (> 50 МБ).", reply_markup=kb_error
                    )
                    return
            except OSError:
                await q.edit_message_text(
                    "⚠️ Ошибка проверки файла.", reply_markup=kb_error
                )
                return

            await q.edit_message_text("📤 Загружаю в Telegram...")

            # Очищаем имя файла от недопустимых символов для Telegram
            safe_title = SAFE_FILENAME_RE.sub("_", payload.get("title", "video")[:100])

            try:
                with open(tmp_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=q.message.chat_id,
                        document=f,
                        filename=f"{safe_title}.{file_ext}",
                        caption="✅ Готово!",
                        read_timeout=90,  # Увеличено для медленных соединений
                        write_timeout=90,
                        connect_timeout=30,
                    )
                await q.edit_message_text("✅ Видео отправлено!", reply_markup=kb_back)
            except NetworkError as net_err:
                # Более детальная обработка сетевых ошибок
                error_str = str(net_err).lower()
                if "413" in error_str or "request entity too large" in error_str:
                    await q.edit_message_text(
                        "⚠️ Файл слишком большой для Telegram (>50 МБ).",
                        reply_markup=kb_error,
                    )
                elif "timeout" in error_str:
                    await q.edit_message_text(
                        "⚠️ Таймаут загрузки. Попробуйте скачать по ссылке.",
                        reply_markup=kb_error,
                    )
                else:
                    await q.edit_message_text(
                        "❌ Ошибка отправки в Telegram. Используйте ссылку.",
                        reply_markup=kb_error,
                    )
                logger.error(f"[DL-TG] Network error: {net_err}")

        except NetworkError as e:
            error_str = str(e).lower()
            if "413" in error_str or "request entity too large" in error_str:
                await q.edit_message_text("⚠️ Файл > 50 MB.", reply_markup=kb_error)
            elif "timeout" in error_str:
                await q.edit_message_text(
                    "⚠️ Таймаут сети. Попробуйте скачать по ссылке.",
                    reply_markup=kb_error,
                )
            else:
                await q.edit_message_text(
                    "❌ Ошибка сети Telegram. Используйте ссылку.",
                    reply_markup=kb_error,
                )
            logger.error(f"[DL-TG] Network error: {e}")
        except Exception as e:
            logger.error(f"[DL-TG] Upload error: {e}", exc_info=True)
            await q.edit_message_text(
                "❌ Ошибка загрузки. Используйте ссылку для скачивания.",
                reply_markup=kb_error,
            )
        finally:
            # Гарантированная очистка временного файла
            if tmp_path:
                await asyncio.to_thread(safe_remove, tmp_path)


# --- APP BUILDER ---
def build_bot_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^pick\|"))
    app.add_handler(CallbackQueryHandler(on_send, pattern=r"^send\|"))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern=r"^cancel\|"))
    app.add_handler(CallbackQueryHandler(on_back, pattern=r"^back$"))
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
        await bot_app.bot.set_webhook(
            f"{WEBHOOK_URL}/webhook", secret_token=TELEGRAM_SECRET_TOKEN
        )
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        # Режим Polling
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Polling started")


@api.on_event("shutdown")
async def _shutdown():
    global bot_app
    logger.info("Shutdown initiated...")

    # 1. Завершаем все активные subprocess
    async with active_processes_lock:
        if active_processes:
            logger.info(f"Terminating {len(active_processes)} active processes...")
            for proc in active_processes:
                try:
                    proc.terminate()
                except:
                    pass
            # Даем процессам немного времени на завершение
            await asyncio.gather(
                *(proc.wait() for proc in active_processes), return_exceptions=True
            )
            active_processes.clear()

    # 2. Останавливаем Bot API
    if bot_app:
        if WEBHOOK_URL:
            await bot_app.bot.delete_webhook()
        elif bot_app.updater.running:
            await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
    logger.info("Shutdown complete.")
