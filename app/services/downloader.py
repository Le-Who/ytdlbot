import os
import uuid
import time
import asyncio
import logging
import shutil
from typing import Optional, Tuple

__all__ = ["MediaSender"]

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.error import NetworkError

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

logger = logging.getLogger("app.services.downloader")

# Maximum photos in a Telegram media group
MAX_TELEGRAM_ALBUM_SIZE = 10


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

    @staticmethod
    async def send_file(
        bot: Bot,
        chat_id: int,
        file_path: str,
        is_audio: bool = False,
        is_gif: bool = False,
        caption: str = "",
        parse_mode: str = None,
        reply_markup=None,
        reply_to_message_id: int = None,
    ) -> bool:
        """
        Sends a file to Telegram.
        """
        try:
            with open(file_path, "rb") as f:
                if is_gif:
                    await bot.send_animation(
                        chat_id=chat_id,
                        animation=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                elif is_audio:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        supports_streaming=True,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
            return True

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
            "-t",
            "60",
            "-i",
            video_path,
            "-c:v",
            "copy",
            "-an",
            gif_path,
        ]

        try:
            # Limit concurrency for CPU-intensive conversions
            async with state.conversion_sem:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    # MP4 encoding is fast, but give it enough time on weak CPU
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=300.0
                    )
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
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

    # ── Slideshow methods ────────────────────────────────────────

    @staticmethod
    async def download_slideshow(
        page_url: str,
    ) -> Tuple[Optional["SlideshowResult"], Optional[str]]:
        """
        Downloads TikTok slideshow images (and optional audio) via gallery-dl.

        Returns:
            (SlideshowResult, None) on success.
            (None, error_message) on failure.
        """
        from app.services.gallery_dl.service import GalleryDlService

        cookies_path = state.ytdlp.cookies_path

        result, error = await asyncio.to_thread(
            GalleryDlService.download_slideshow, page_url, cookies_path
        )

        return result, error

    @staticmethod
    async def images_to_video(
        images: list[str],
        audio_path: Optional[str] = None,
    ) -> Optional[str]:
        """
        Converts a list of images (+ optional audio) into a slideshow MP4.

        Each image is displayed for ~3 seconds. If audio exists, the video
        duration matches the audio length (with -shortest).

        Returns:
            Path to output MP4, or None on failure.
        """
        if not images:
            return None

        output_dir = TEMP_DIR
        output_path = os.path.join(output_dir, f"slideshow_{uuid.uuid4().hex}.mp4")

        # Create a temporary concat list for ffmpeg
        concat_file = os.path.join(output_dir, f"concat_{uuid.uuid4().hex}.txt")
        try:
            with open(concat_file, "w", encoding="utf-8") as f:
                for img in images:
                    # Escape single quotes in path for ffmpeg concat
                    escaped = img.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
                    f.write("duration 3\n")
                # Repeat last image to avoid ffmpeg cutting it short
                if images:
                    escaped = images[-1].replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            # Build ffmpeg command
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
            ]

            if audio_path and os.path.exists(audio_path):
                cmd.extend(["-i", audio_path])
                cmd.extend([
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale='min(1080,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_path,
                ])
            else:
                cmd.extend([
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale='min(1080,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-movflags", "+faststart",
                    output_path,
                ])

            async with state.conversion_sem:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=300.0
                    )
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    logger.error("FFmpeg slideshow conversion timed out")
                    return None

            if proc.returncode != 0:
                logger.error(
                    f"FFmpeg slideshow failed: {stderr.decode('utf-8', errors='ignore')}"
                )
                return None

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return None

            return output_path

        except Exception as e:
            logger.error(f"Slideshow conversion exception: {e}", exc_info=True)
            return None
        finally:
            safe_remove(concat_file)

    @staticmethod
    async def send_slideshow_photos(
        bot: Bot,
        chat_id: int,
        images: list[str],
        caption: str = "",
        parse_mode: str = None,
        reply_to_message_id: int = None,
    ) -> bool:
        """
        Sends images as a Telegram media group (photo album).

        Max 10 photos per group. Returns True on success.
        """
        if not images:
            return False

        photos_to_send = images[:MAX_TELEGRAM_ALBUM_SIZE]

        file_handles = []
        try:
            media = []
            for i, img_path in enumerate(photos_to_send):
                fh = open(img_path, "rb")
                file_handles.append(fh)
                media.append(
                    InputMediaPhoto(
                        media=fh,
                        caption=caption if i == 0 else None,
                        parse_mode=parse_mode if i == 0 else None,
                    )
                )

            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                reply_to_message_id=reply_to_message_id,
            )
            return True

        except NetworkError:
            logger.warning("Network error sending slideshow photos")
            return False
        except Exception as e:
            logger.error(f"Slideshow send error: {e}", exc_info=True)
            return False
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

    @staticmethod
    def cleanup_slideshow(result) -> None:
        """Removes all downloaded slideshow files and their directory."""
        if not result or not result.images:
            return
        # All slideshow files are in the same parent directory
        parent_dir = os.path.dirname(result.images[0])
        if parent_dir and os.path.isdir(parent_dir) and "slideshow_" in parent_dir:
            try:
                shutil.rmtree(parent_dir, ignore_errors=True)
                logger.info(f"[CLEANUP] Removed slideshow dir: {parent_dir}")
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to remove slideshow dir: {e}")
