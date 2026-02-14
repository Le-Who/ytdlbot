import os
import uuid
import time
import asyncio
import logging
import shutil
from typing import Optional, Tuple, AsyncGenerator

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError

from app.core import state
from app.core.config import TEMP_DIR
from app.core.utils import (
    safe_remove,
    run_subprocess,
    render_progressbar,
    PROGRESS_RE,
    PROGRESS_DETAILS_RE,
)
from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID

logger = logging.getLogger("app.services.downloader")

class MediaSender:
    """
    Service to handle downloading media via yt-dlp, converting to GIF,
    and sending files to Telegram.
    """

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
            max_filesize=50,
            use_aria2=True,
        )

        kb_cancel = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{token}")]]
        )

        try:
            async for proc, stderr in run_subprocess(cmd):
                last_update = 0
                download_start = time.time()
                max_download_time = 600

                # Clear previous cancel state for this token
                if token in state.cancel_cache:
                    del state.cancel_cache[token]

                try:
                    # Performance optimization: use asyncio.timeout with reschedule
                    # instead of creating a new Task for every line read with wait_for.
                    async with asyncio.timeout(300.0) as cm:
                        while True:
                            if state.cancel_cache.get(token):
                                logger.info(f"[DL-TG] Cancelled by user: {token}")
                                return None, "❌ Загрузка отменена пользователем."

                            if time.time() - download_start > max_download_time:
                                logger.warning("[DL-TG] Download timeout exceeded")
                                return None, "⚠️ Время ожидания загрузки истекло."

                            line = await proc.stdout.readline()

                            # Reset inactivity timer
                            cm.reschedule(asyncio.get_running_loop().time() + 300.0)

                            if not line:
                                break

                            # Optimization: Skip decoding lines that are not progress updates
                            if not progress_callback:
                                continue

                            if b"[download]" not in line or b"%" not in line:
                                continue

                            line_str = line.decode("utf-8", errors="ignore").strip()

                            if progress_callback and "[download]" in line_str and "%" in line_str:
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
                except asyncio.TimeoutError:
                    if proc.returncode is None:
                        logger.warning("[DL-TG] Inactivity timeout (300s) exceeded")
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return None, "⚠️ Время ожидания загрузки истекло (нет данных)."

                await proc.wait()
                if proc.returncode != 0:
                    err = b"".join(stderr).decode("utf-8", errors="ignore").lower()
                    logger.error(f"[DL-TG] yt-dlp failed: {err}")

                    if "file larger" in err or "filesize" in err:
                        return None, "⚠️ Файл слишком большой (>50 МБ)."
                    elif "sign in" in err or "cookies" in err:
                        return None, "⚠️ Требуется авторизация (Sign-in required)."
                    elif "requested format is not available" in err:
                         return None, "⚠️ Формат недоступен. Попробуйте другое качество."
                    else:
                        return None, "⚠️ Ошибка загрузки. Попробуйте другое качество или ссылку."

            # Verify file
            try:
                if not os.path.exists(tmp_path):
                     return None, "⚠️ Файл не был создан."
                     
                file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                if file_size > 49.9 * 1024 * 1024:
                    await asyncio.to_thread(safe_remove, tmp_path)
                    return None, "⚠️ Файл слишком большой (> 50 МБ)."
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

    @staticmethod
    async def send_file(
        bot: Bot,
        chat_id: int,
        file_path: str,
        is_audio: bool = False,
        is_gif: bool = False,
        caption: str = "",
        reply_markup=None,
        reply_to_message_id: int = None,
    ) -> bool:
        """
        Sends a file to Telegram.
        """
        try:
            f = await asyncio.to_thread(open, file_path, "rb")
            try:
                if is_gif:
                    await bot.send_animation(
                        chat_id=chat_id,
                        animation=f,
                        caption=caption,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                elif is_audio:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        supports_streaming=True,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                return True
            finally:
                await asyncio.to_thread(f.close)

        except NetworkError:
            logger.warning(f"Network error sending file {file_path}")
            return False
        except Exception as e:
            logger.error(f"Send error: {e}", exc_info=True)
            return False

    @staticmethod
    async def convert_to_gif_ffmpeg(video_path: str) -> Optional[str]:
        """
        Converts a video to a mute MP4 (Telegram treats as GIF).
        Uses a lock to prevent CPU overload.
        """
        if not video_path or not os.path.exists(video_path):
            return None

        # Output as MP4, not GIF. Telegram send_animation supports MP4.
        gif_path = video_path.rsplit(".", 1)[0] + "_gif.mp4"
        
        # Optimization: Stream Copy (Fastest)
        # -c:v copy: Copy video stream directly (no re-encoding, original quality)
        # -an: Remove audio
        # -t 60: Safety cut (though usually redundant if copy)
        # This resolves "Video has sound" AND "Re-encoding makes it bigger/worse" issues.
        # It is instant (IO bound).
        
        cmd = [
            "ffmpeg",
            "-y",
            "-t", "60", 
            "-i", video_path,
            "-c:v", "copy",
            "-an",
            gif_path
        ]
        
        try:
            # Acquire lock to ensure we only burn CPU for one task at a time
            async with state.conversion_lock:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    # MP4 encoding is fast, but give it enough time on weak CPU
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except:
                        pass
                    logger.error("FFmpeg conversion timed out")
                    return None
            
            if proc.returncode != 0:
                logger.error(f"FFmpeg conversion failed: {stderr.decode()}")
                return None
            
            if not os.path.exists(gif_path) or os.path.getsize(gif_path) == 0:
                return None
                
            return gif_path
        except Exception as e:
            logger.error(f"FFmpeg exception: {e}")
            return None
