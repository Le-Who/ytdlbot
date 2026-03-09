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


from app.core import state
from app.core.config import TEMP_DIR, MAX_TG_UPLOAD_MB, DL_TIMEOUT_TELEGRAM
from app.core.utils import safe_remove
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


# Late import to avoid circular dep — metrics is a lightweight singleton
def _metrics():
    from app.core.metrics import metrics

    return metrics


def _safe_remove_info_json(path):
    """Remove cached info JSON after download (prevent /tmp fill)."""
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


class VideoDownloader:
    """Downloads video/audio files via yt-dlp subprocess."""

    @staticmethod
    async def download_video(
        page_url: str,
        format_id: str,
        height: Optional[int],
        token: str,
        info_json_path: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Downloads a video.

        Args:
            page_url: URL to download.
            format_id: yt-dlp format ID.
            height: Video height (for filename/metadata).
            token: Unique token for cancellation and file cache.
            info_json_path: Optional path to cached extraction JSON
                (--load-info-json to skip re-extraction).

        Returns:
            (file_path, error_message)
            If success: file_path is str, error_message is None.
            If fail: file_path is None, error_message is str.
        """
        if format_id == "tikwm_fallback":
            from app.services.tikwm import TikWMService

            _metrics().downloads_total.inc(platform="tiktok_fallback")
            _metrics().active_downloads.inc()
            try:
                res, err = await TikWMService.download_video(page_url)
                if res and not err:
                    _metrics().downloads_success.inc(platform="tiktok_fallback")
                    state.file_cache[token] = res
                else:
                    _metrics().downloads_failed.inc(platform="tiktok_fallback")
                return res, err
            finally:
                _metrics().active_downloads.dec()

        if format_id == "gallerydl_fallback":
            from app.services.gallery_dl.service import GalleryDlService

            _metrics().downloads_total.inc(platform="tiktok_fallback")
            _metrics().active_downloads.inc()
            try:
                res, err = await asyncio.to_thread(
                    GalleryDlService.download_video,
                    page_url,
                    state.ytdlp.cookies_path,
                    state.ytdlp.tiktok_proxy,
                )
                if res and not err:
                    _metrics().downloads_success.inc(platform="tiktok_fallback")
                    state.file_cache[token] = res
                else:
                    _metrics().downloads_failed.inc(platform="tiktok_fallback")
                return res, err
            finally:
                _metrics().active_downloads.dec()

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
            logger.info("Reusing cached file", extra={"path": cached_path})
            _metrics().cache_hits.inc(cache="file_cache")
            return cached_path, None

        tmp_path = os.path.join(tmp_dir, f"ytdl_{uuid.uuid4().hex}.{file_ext}")

        cmd = state.ytdlp.build_command(
            page_url,
            format_id,
            height,
            output=tmp_path,
            max_filesize=MAX_TG_UPLOAD_MB,
            use_aria2=True,
            info_json_path=info_json_path,
        )

        _dl_start = time.time()
        try:
            _metrics().downloads_total.inc(platform="telegram")
            _metrics().active_downloads.inc()
            async with run_subprocess(cmd) as handle:
                proc = handle.proc
                assert proc.stdout is not None
                stderr = handle.stderr_data
                download_start = time.time()
                max_download_time = DL_TIMEOUT_TELEGRAM

                # Clear previous cancel state for this token
                state.cancel_cache.pop(token, None)

                # Drain stdout; check cancel/timeout every 2s
                while True:
                    if state.cancel_cache.get(token):
                        logger.info("Cancelled by user", extra={"token": token})
                        return None, "❌ Загрузка отменена пользователем."

                    if time.time() - download_start > max_download_time:
                        logger.warning("[DL-TG] Download timeout exceeded")
                        return None, "⚠️ Время ожидания загрузки истекло."

                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue

                    if not line:
                        break

                await proc.wait()

                if proc.returncode != 0:
                    _metrics().downloads_failed.inc(platform="telegram")
                    _metrics().active_downloads.dec()
                    err = b"".join(stderr).decode("utf-8", errors="ignore").lower()
                    logger.error("yt-dlp download failed", extra={"stderr": err})

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
            _metrics().downloads_success.inc(platform="telegram")
            _metrics().active_downloads.dec()
            _metrics().download_duration.observe(
                time.time() - _dl_start, platform="telegram"
            )
            return tmp_path, None

        except Exception as e:
            logger.error("Download exception", extra={"error": str(e)}, exc_info=True)
            _metrics().downloads_failed.inc(platform="telegram")
            _metrics().active_downloads.dec()
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
