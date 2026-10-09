"""Video download service + MediaSender backward-compatible facade.

The actual implementation is split across focused service modules:
- sender.py    → TelegramSender (send_file, send_slideshow_photos)
- converter.py → MediaConverter (convert_to_gif_ffmpeg, images_to_video)
- slideshow.py → SlideshowPipeline (download_slideshow, cleanup_slideshow)

This module retains download_video and provides MediaSender as a facade
so existing callers (callbacks.py, group_logic.py, tests) don't break.
"""

import asyncio
import io
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from app.core import state
from app.core.config import (
    DL_TIMEOUT_TELEGRAM,
    MAX_TG_UPLOAD_MB,
    TEMP_DIR,
    YOUTUBE_PIPE_MODE,
)
from app.core.policy import size_allowed
from app.core.process import (
    ProcessOwnerCancelled,
    current_process_owner,
    run_subprocess,
)
from app.core.utils import safe_remove
from app.services.converter import MediaConverter

# Re-export decomposed services for backward compatibility
from app.services.sender import MAX_TELEGRAM_ALBUM_SIZE, TelegramSender
from app.services.slideshow import SlideshowPipeline

__all__ = [
    "MAX_TELEGRAM_ALBUM_SIZE",
    "MediaConverter",
    "MediaSender",
    "SlideshowPipeline",
    "TelegramSender",
    "VideoDownloader",
]

logger = logging.getLogger("app.services.downloader")


# Late import to avoid circular dep — metrics is a lightweight singleton
def _metrics():
    from app.core.metrics import metrics

    return metrics


class VideoDownloader:
    """Downloads video/audio files via yt-dlp subprocess."""

    @staticmethod
    async def download_video(
        page_url: str,
        format_id: str,
        height: int | None,
        token: str,
        info_json_path: str | None = None,
        fallback_clients: bool = False,
        progress_callback: Callable[[float, str], Awaitable[None]] | None = None,
        section: str | None = None,
    ) -> tuple[str | io.BytesIO | None, str | None]:
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
            (file_path_or_buffer, error_message)
            If success: file_path_or_buffer is str or io.BytesIO, error_message is None.
            If fail: file_path_or_buffer is None, error_message is str.
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

        # Ensure we only use pipe for standard videos, not audio or GIF converting target
        use_pipe = YOUTUBE_PIPE_MODE and not is_gif and not is_audio

        # Check if we already have this file in cache (e.g. for GIF conversion reuse)
        cached_path = state.file_cache.get(token)
        if cached_path and isinstance(cached_path, str) and os.path.exists(cached_path):
            logger.info("Reusing cached file", extra={"path": cached_path})
            _metrics().cache_hits.inc(cache="file_cache")
            return cached_path, None

        tmp_path = os.path.join(tmp_dir, f"ytdl_{uuid.uuid4().hex}.{file_ext}")
        output_path = "-" if use_pipe else tmp_path

        cmd = state.ytdlp.build_command(
            page_url,
            format_id,
            height,
            output=output_path,
            max_filesize=MAX_TG_UPLOAD_MB,
            use_aria2=not use_pipe,  # aria2c doesn't support stdout piping
            info_json_path=info_json_path,
            fallback_clients=fallback_clients,
            pipe_mode=use_pipe,
            section=section,
        )
        logger.info(
            "yt-dlp download cmd: %s",
            " ".join(str(a) for a in cmd),
        )

        _dl_start = time.time()
        try:
            _metrics().downloads_total.inc(platform="telegram")
            _metrics().active_downloads.inc()

            PROGRESS_RE = re.compile(rb"\[download\]\s+([\d\.]+)%[^E]*ETA\s+([\d:]+)")

            async def _handle_progress(line: bytes) -> None:
                if not progress_callback:
                    return
                m = PROGRESS_RE.search(line)
                if m:
                    try:
                        pct = float(m.group(1))
                        eta = f"ETA {m.group(2).decode()}"
                        await progress_callback(pct, eta)
                    # Progress callbacks are optional caller adapters; their
                    # failures must not interrupt the supervised media download.
                    except Exception as error:  # noqa: BLE001
                        logger.debug(
                            "Download progress callback failed",
                            extra={"error_type": type(error).__name__},
                        )

            def on_stderr(line: bytes) -> None:
                if progress_callback:
                    asyncio.create_task(_handle_progress(line))

            async with run_subprocess(
                cmd,
                stderr_callback=on_stderr,
                owner=current_process_owner() or token,
            ) as handle:
                proc = handle.proc
                assert proc.stdout is not None
                stderr = handle.stderr_data
                download_start = time.time()
                max_download_time = DL_TIMEOUT_TELEGRAM

                buffer = io.BytesIO() if use_pipe else None
                last_cancel_check = 0.0

                # The callback cancels this stable token owner immediately.
                # This short poll remains as a fallback for non-Bot callers
                # that only update the shared cancellation cache.
                while True:
                    now = time.time()
                    if now - last_cancel_check > 0.25:
                        if await state.cancel_cache.get(token):
                            logger.info("Cancelled by user", extra={"token": token})
                            return None, "❌ Загрузка отменена пользователем."
                        last_cancel_check = now

                    if now - download_start > max_download_time:
                        logger.warning("[DL-TG] Download timeout exceeded")
                        return None, "⚠️ Время ожидания загрузки истекло."

                    try:
                        if use_pipe:
                            assert buffer is not None
                            chunk = await asyncio.wait_for(
                                proc.stdout.read(65536), timeout=0.25
                            )
                            if not chunk:
                                break
                            buffer.write(chunk)
                        else:
                            line = await asyncio.wait_for(
                                proc.stdout.readline(), timeout=0.25
                            )
                            if not line:
                                break
                    except TimeoutError:
                        if proc.returncode is not None:
                            break
                        continue

                await proc.wait()

                if await state.cancel_cache.get(token):
                    return None, "❌ Загрузка отменена пользователем."

                if proc.returncode != 0:
                    _metrics().downloads_failed.inc(platform="telegram")
                    err_text = b"".join(stderr).decode("utf-8", errors="ignore")
                    logger.error("yt-dlp download failed", extra={"stderr": err_text})

                    from app.services.ytdlp.exceptions import (
                        AccessDeniedError,
                        ExtractionError,
                        VideoNotFoundError,
                        map_ytdlp_error,
                    )

                    mapped_err = map_ytdlp_error(err_text, page_url)

                    if (
                        "file larger" in err_text.lower()
                        or "filesize" in err_text.lower()
                    ):
                        return (
                            None,
                            f"⚠️ Файл слишком большой (>{MAX_TG_UPLOAD_MB} МБ).",
                        )

                    if isinstance(mapped_err, AccessDeniedError):
                        # Surface the specific message (e.g. VK auth instruction)
                        return None, str(mapped_err)
                    elif isinstance(mapped_err, VideoNotFoundError):
                        return (
                            None,
                            "⚠️ Видео не найдено или скрыто настройками приватности.",
                        )
                    elif isinstance(
                        mapped_err, ExtractionError
                    ) and "формат недоступен" in str(mapped_err):
                        return None, "⚠️ Формат недоступен. Попробуйте другое качество."
                    else:
                        return (
                            None,
                            "⚠️ Ошибка загрузки. Попробуйте другое качество или ссылку.",
                        )

            # Verify file
            if use_pipe:
                assert buffer is not None
                buffer.seek(0)
                file_size = buffer.getbuffer().nbytes
                if not size_allowed(file_size, target="telegram"):
                    return None, f"⚠️ Файл слишком большой (> {MAX_TG_UPLOAD_MB} МБ)."
                # For pipes, we don't save to file_cache (no physical path)
                _metrics().downloads_success.inc(platform="telegram")
                _metrics().download_duration.observe(
                    time.time() - _dl_start, platform="telegram"
                )
                return buffer, None
            else:
                try:
                    if not os.path.exists(tmp_path):
                        return None, "⚠️ Файл не был создан."

                    file_size = await asyncio.to_thread(os.path.getsize, tmp_path)
                    if not size_allowed(file_size, target="telegram"):
                        await asyncio.to_thread(safe_remove, tmp_path)
                        return (
                            None,
                            f"⚠️ Файл слишком большой (> {MAX_TG_UPLOAD_MB} МБ).",
                        )
                except OSError:
                    await asyncio.to_thread(safe_remove, tmp_path)
                    return None, "⚠️ Ошибка проверки файла."

                # Save to cache
                state.file_cache[token] = tmp_path
                _metrics().downloads_success.inc(platform="telegram")
                _metrics().download_duration.observe(
                    time.time() - _dl_start, platform="telegram"
                )
                return tmp_path, None

        except ProcessOwnerCancelled:
            return None, "❌ Загрузка отменена пользователем."
        except Exception as e:
            logger.exception("Download exception", extra={"error": str(e)})
            _metrics().downloads_failed.inc(platform="telegram")
            await asyncio.to_thread(safe_remove, tmp_path)
            return None, "⚠️ Внутренняя ошибка при загрузке."

        finally:
            _metrics().active_downloads.dec()


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
