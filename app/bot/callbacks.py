import os
import uuid
import asyncio
import time
import logging
import tempfile
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import NetworkError

from app.core import state
from app.core.config import BASE_URL, LINK_TTL_MINUTES, ENABLE_TELEGRAM_UPLOAD
from app.core.utils import check_rate_limit, safe_remove, run_subprocess, render_progressbar, PROGRESS_RE
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from app.bot.keyboards import build_format_keyboard

logger = logging.getLogger("app.bot.callbacks")

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

    cached = state.info_cache.get(page_url)
    if not cached:
        try:
            await q.edit_message_text("⏳ Кэш истек. Обновляю данные...")
            async with state.parsing_sem:
                title, formats, special_format, duration = await asyncio.to_thread(
                    state.ytdlp.list_formats, page_url
                )
            state.info_cache[page_url] = (title, formats, special_format, duration)
        except Exception as e:
            logger.error(f"[ON_BACK] Refresh error: {e}")
            await q.edit_message_text(
                "⚠️ Ошибка обновления данных. Отправьте ссылку заново."
            )
            return
    else:
        title, formats, special_format, duration = cached

    reply_markup = build_format_keyboard(formats, special_format)

    await q.edit_message_text(
        f"📹 <b>{html.escape(title)}</b>\n⏱ {duration}",
        reply_markup=reply_markup,
        parse_mode="HTML",
    )

async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Подготовка ссылки...")

    if not q.data:
        return

    try:
        _, format_id = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_pick: {e}")
        return

    data = context.user_data
    if not data.get("page_url"):
        await q.edit_message_text(
            "⚠️ Данные устарели. Пожалуйста, отправьте ссылку на видео еще раз."
        )
        return

    token = uuid.uuid4().hex
    state.link_cache[token] = {
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
    q = update.callback_query
    await q.answer("🚫 Отменяю...")

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
        state.cancel_cache[token] = True
        await q.edit_message_text("❌ Загрузка отменена пользователем.")
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_cancel: {e}")

async def on_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚀 Загрузка началась")
    user_id = q.from_user.id

    if not check_rate_limit(user_id, limit=3):
        await q.edit_message_text("⚠️ Слишком часто скачиваете. Подождите.")
        return

    if not q.data:
        return

    try:
        _, token = q.data.split("|", 1)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid callback data in on_send: {e}")
        return

    payload = state.link_cache.get(token)
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
    kb_cancel = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{token}")]]
    )

    if state.tasks_sem.locked():
        await q.edit_message_text(
            "⚠️ Очередь переполнена. Скачайте по ссылке.", reply_markup=kb_error
        )
        return

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

    async with state.tasks_sem:
        await q.edit_message_text("⏳ Начинаю загрузку...")
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

        cmd = state.ytdlp.build_command(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            output=tmp_path,
            max_filesize=50,
            use_aria2=True,
        )

        try:
            async for proc, stderr in run_subprocess(cmd):
                last_update = 0
                download_start = time.time()
                max_download_time = 600

                if token in state.cancel_cache:
                    del state.cancel_cache[token]

                while True:
                    if state.cancel_cache.get(token):
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
                                except Exception as e:
                                    logger.warning(
                                        f"Failed to parse progress or update message: {e}"
                                    )

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

            if is_gif:
                video_tmp = tmp_path.replace(".gif", "_video.mp4")
                if await asyncio.to_thread(os.path.exists, video_tmp):
                     pass 

            try:
                file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                if file_size > 49.9 * 1024 * 1024:
                    await q.edit_message_text("⚠️ Файл слишком большой (> 50 МБ).", reply_markup=kb_error)
                    return
            except OSError:
                await q.edit_message_text("⚠️ Ошибка проверки файла.", reply_markup=kb_error)
                return

            await q.edit_message_text("📤 Отправляю в Telegram...")

            try:
                f = await asyncio.to_thread(open, tmp_path, "rb")
                try:
                    if is_gif:
                        await context.bot.send_animation(
                            chat_id=q.message.chat_id,
                            animation=f,
                            caption="🎬",
                        )
                    elif is_audio:
                         await context.bot.send_audio(
                            chat_id=q.message.chat_id,
                            audio=f,
                            caption="🎵",
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=q.message.chat_id,
                            video=f,
                            caption="📹",
                            supports_streaming=True,
                        )
                finally:
                    await asyncio.to_thread(f.close)
                await q.delete_message()
            except NetworkError:
                await q.edit_message_text("⚠️ Ошибка сети при отправке (возможно, файл слишком большой).", reply_markup=kb_error)
            except Exception as e:
                logger.error(f"Send error: {e}", exc_info=True)
                await q.edit_message_text("⚠️ Ошибка при отправке файла.", reply_markup=kb_error)

        except Exception as e:
            logger.error(f"TG download error: {e}", exc_info=True)
            await q.edit_message_text("⚠️ Внутренняя ошибка.", reply_markup=kb_error)
        finally:
            await asyncio.to_thread(safe_remove, tmp_path)
