"""Video download service + MediaSender backward-compatible facade.

The actual implementation is split across focused service modules:
- sender.py    → TelegramSender (send_file, send_slideshow_photos)
- converter.py → MediaConverter (convert_to_gif_ffmpeg, images_to_video)
- slideshow.py → SlideshowPipeline (download_slideshow, cleanup_slideshow)

This module retains download_video and provides MediaSender as a facade
so existing callers (callbacks.py, group_logic.py, tests) don't break.
"""

import os
import uuid
import time
import asyncio
import logging
from typing import Optional, Tuple

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from app.core import state
from app.core.config import TEMP_DIR, MAX_TG_UPLOAD_MB, DL_TIMEOUT_TELEGRAM
from app.core.utils import (
    safe_remove,
    render_progressbar,
    PROGRESS_RE,
    PROGRESS_DETAILS_RE,
)
from app.core.process import run_subprocess
from app.core.policy import size_allowed
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID

# Re-export decomposed services for backward compatibility
from app.services.sender import TelegramSender, MAX_TELEGRAM_ALBUM_SIZE
from app.services.converter import MediaConverter
from app.services.slideshow import SlideshowPipeline

__all__ = [
    "MediaSender",
    "VideoDownloader",
    "TelegramSender",
    "MediaConverter",
    "SlideshowPipeline",
    "MAX_TELEGRAM_ALBUM_SIZE",
]

logger = logging.getLogger("app.services.downloader")


class VideoDownloader:
    """Downloads video/audio files via yt-dlp subprocess."""

    @staticmethod
    async def download_video(
        page_url: str,
        format_id: str,
        height: Optional[int],
        token: str,
        progress_callback=None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Downloads a video.

        Args:
            page_url: URL to download.
            format_id: yt-dlp format ID.
            height: Video height (for filename/metadata).
            token: Unique token for cancellation and file cache.
            progress_callback: Async function(text, markup) to update UI.

        Returns:
            (file_path, error_message)
            If success: file_path is str, error_message is None.
            If fail: file_path is None, error_message is str.
        """
        tmp_dir = TEMP_DIR
        is_gif = format_id == GIF_FORMAT_ID
        is_audio = format_id == AUDIO_FORMAT_ID

        if is_gif:
            file_ext = "gif"
        elif is_audio:
            file_ext = "mp3"
        else:
            file_ext = "mp4"

        # Check if we already have this file in cache (e.g. for GIF conversion reuse)
        cached_path = state.file_cache.get(token)
        if cached_path and os.path.exists(cached_path):
            logger.info(f"[CACHE] Reusing downloaded file: {cached_path}")
            return cached_path, None

        tmp_path = os.path.join(tmp_dir, f"ytdl_{uuid.uuid4().hex}.{file_ext}")

        cmd = state.ytdlp.build_command(
            page_url,
            format_id,
            height,
            output=tmp_path,
            max_filesize=MAX_TG_UPLOAD_MB,
            use_aria2=True,
        )

        kb_cancel = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{token}")]]
        )

        try:
            async with run_subprocess(cmd) as handle:
                proc = handle.proc
                stderr = handle.stderr_data
                last_update = 0
                download_start = time.time()
                max_download_time = DL_TIMEOUT_TELEGRAM

                # Clear previous cancel state for this token
                state.cancel_cache.pop(token, None)

                while True:
                    if state.cancel_cache.get(token):
                        logger.info(f"[DL-TG] Cancelled by user: {token}")
                        return None, "❌ Загрузка отменена пользователем."

                    if time.time() - download_start > max_download_time:
                        logger.warning("[DL-TG] Download timeout exceeded")
                        return None, "⚠️ Время ожидания загрузки истекло."

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

                    if (
                        progress_callback
                        and "[download]" in line_str
                        and "%" in line_str
                    ):
                        now = time.time()
                        if now - last_update > 3.0:
                            match = PROGRESS_RE.search(line_str)
                            if match:
                                try:
                                    percent = float(match.group(1))
                                    details = ""

                                    det_match = PROGRESS_DETAILS_RE.search(line_str)
                                    if det_match:
                                        speed = det_match.group(1)
                                        eta = det_match.group(2)
                                        details = f"\n🚀 {speed} • ⏱ ETA {eta}"

                                    await progress_callback(
                                        f"⏳ Скачиваю: {render_progressbar(percent)}{details}\n❌ Нажмите отмена, если передумали.",
                                        kb_cancel,
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
                        return None, f"⚠️ Файл слишком большой (>{MAX_TG_UPLOAD_MB} МБ)."
                    elif "sign in" in err or "cookies" in err:
                        return None, "⚠️ Требуется авторизация (Sign-in required)."
                    elif "requested format is not available" in err:
                        return None, "⚠️ Формат недоступен. Попробуйте другое качество."
                    else:
                        return (
                            None,
                            "⚠️ Ошибка загрузки. Попробуйте другое качество или ссылку.",
                        )

            # Verify file
            try:
                if not os.path.exists(tmp_path):
                    return None, "⚠️ Файл не был создан."

                file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                if not size_allowed(file_size, target="telegram"):
                    await asyncio.to_thread(safe_remove, tmp_path)
                    return None, f"⚠️ Файл слишком большой (> {MAX_TG_UPLOAD_MB} МБ)."
            except OSError:
                await asyncio.to_thread(safe_remove, tmp_path)
                return None, "⚠️ Ошибка проверки файла."

            # Save to cache
            state.file_cache[token] = tmp_path
            return tmp_path, None

        except Exception as e:
            logger.error(f"Download exception: {e}", exc_info=True)
            await asyncio.to_thread(safe_remove, tmp_path)
            return None, "⚠️ Внутренняя ошибка при загрузке."


class MediaSender:
    """Backward-compatible facade delegating to focused service classes.

    Existing code can continue to use `MediaSender.download_video(...)` etc.
    New code should import from the specific service modules directly.
    """

    # VideoDownloader
    download_video = VideoDownloader.download_video

    # TelegramSender
    send_file = TelegramSender.send_file
    send_slideshow_photos = TelegramSender.send_slideshow_photos

    # MediaConverter
    convert_to_gif_ffmpeg = MediaConverter.convert_to_gif_ffmpeg
    images_to_video = MediaConverter.images_to_video

    # SlideshowPipeline
    download_slideshow = SlideshowPipeline.download_slideshow
    cleanup_slideshow = SlideshowPipeline.cleanup_slideshow
