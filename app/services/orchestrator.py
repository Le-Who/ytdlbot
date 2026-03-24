import asyncio
import json
import os
import io
import logging
from typing import Optional, Callable, Awaitable, Union

from telegram.constants import ChatAction
from telegram import Bot

from app.core import state
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import safe_remove
from app.core.policy import size_allowed
from app.core.texts import Texts
from app.core.models import DownloadContext
from app.constants import GIF_FORMAT_ID, AUDIO_FORMAT_ID
from app.services.downloader import MediaSender
from app.services.tikwm import TikWMService
from app.services.gallery_dl.service import GalleryDlService
from app.services.pinterest import PinterestNativeService

logger = logging.getLogger("app.services.orchestrator")

# Telegram-compatible video codecs — anything else needs re-encoding
_TG_SAFE_CODECS = {"h264", "mpeg4"}


async def _extract_video_meta(
    file_path: str | object,
    info_json_path: Optional[str] = None,
) -> dict:
    """Extract duration/width/height/vcodec for Telegram send_video.

    Strategy: try info JSON first (zero-cost), fall back to ffprobe.
    Returns dict with keys: duration, width, height (all Optional[int]),
    vcodec (Optional[str]).
    """
    meta: dict = {"duration": None, "width": None, "height": None, "vcodec": None}

    if info_json_path and os.path.exists(info_json_path):
        try:
            with open(info_json_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            dur = info.get("duration")
            if dur:
                meta["duration"] = int(float(dur))
            if info.get("width"):
                meta["width"] = int(info["width"])
            if info.get("height"):
                meta["height"] = int(info["height"])
            meta["vcodec"] = info.get("vcodec")
            if meta["duration"]:
                return meta
        except Exception:
            pass

    if not isinstance(file_path, str):
        return meta

    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if stdout:
            probe = json.loads(stdout)
            fmt = probe.get("format", {})
            dur = fmt.get("duration")
            if dur:
                meta["duration"] = int(float(dur))
            stream: dict = next(
                (s for s in probe.get("streams", []) if s.get("codec_type") == "video"),
                {},
            )
            if stream.get("width"):
                meta["width"] = int(stream["width"])
            if stream.get("height"):
                meta["height"] = int(stream["height"])
            if stream.get("codec_name"):
                meta["vcodec"] = stream["codec_name"].lower()
            if stream.get("pix_fmt"):
                meta["pix_fmt"] = stream["pix_fmt"].lower()
            if stream.get("codec_tag_string"):
                meta["codec_tag"] = stream["codec_tag_string"].lower()
    except Exception as e:
        logger.warning("ffprobe meta extraction failed for %s: %s", file_path, e)

    return meta


async def _ensure_telegram_compatible(file_path: str) -> str:
    """Re-encode video to H.264/AAC if its codec is not Telegram-compatible.

    TikTok CDN often serves HEVC (H.265) videos which Telegram clients
    cannot play inline.  This function detects incompatible codecs via
    ffprobe and re-encodes with libx264 ultrafast.  H.264 videos pass
    through untouched (zero-cost happy path).

    Returns:
        Original path if already compatible, or path to re-encoded file.
    """
    meta = await _extract_video_meta(file_path)
    vcodec = meta.get("vcodec")
    pix_fmt = meta.get("pix_fmt")

    # If ffprobe failed completely (vcodec=None), it might be a broken container.
    # We should attempt re-encoding rather than passing it raw to Telegram.
    is_safe_codec = (vcodec is not None) and (vcodec in _TG_SAFE_CODECS)
    # Telegram cannot natively play 10-bit H.264 (yuv420p10le).
    is_safe_pix_fmt = not pix_fmt or "10" not in pix_fmt

    if is_safe_codec and is_safe_pix_fmt:
        return file_path  # already compatible — no re-encode

    logger.warning(
        "Video codec '%s' (pix_fmt: '%s') is not Telegram-compatible, re-encoding to H.264",
        vcodec,
        pix_fmt,
    )

    re_encoded = file_path.rsplit(".", 1)[0] + "_h264.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        file_path,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        re_encoded,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)

        if proc.returncode != 0:
            logger.error(
                "H.264 re-encode failed: %s",
                stderr.decode("utf-8", errors="ignore")[-500:],
            )
            safe_remove(re_encoded)
            return file_path  # fall back to original

        if not os.path.exists(re_encoded) or os.path.getsize(re_encoded) == 0:
            safe_remove(re_encoded)
            return file_path

        logger.info(
            "Re-encoded %s -> %s (codec %s -> h264)",
            file_path,
            re_encoded,
            vcodec,
        )
        safe_remove(file_path)  # clean up the original
        return re_encoded

    except asyncio.TimeoutError:
        logger.error("H.264 re-encode timed out")
        safe_remove(re_encoded)
        return file_path
    except Exception as e:
        logger.error("H.264 re-encode exception: %s", e)
        safe_remove(re_encoded)
        return file_path


class DownloadOrchestrator:
    """Encapsulates the download business logic, leaving UI components thin."""

    @staticmethod
    async def process_download(
        token: str,
        chat_id: int,
        bot: Bot,
        payload: DownloadContext,
        fmt_size: Optional[int],
        update_ui: Callable[[str, Optional[object]], Awaitable[None]],
        kb_error: object,
    ) -> bool:
        """Process download with concurrency checks, sizes, and Telegram upload."""

        if state.tasks_sem.locked():
            await update_ui(Texts.QUEUE_FULL, kb_error)
            return False

        await state.tasks_sem.acquire()

        try:
            if not size_allowed(fmt_size, target="telegram"):
                mb = (fmt_size or 0) / (1024 * 1024)
                await update_ui(
                    Texts.FILE_TOO_BIG.format(size_mb=mb, max_mb=MAX_TG_UPLOAD_MB),
                    kb_error,
                )
                return False

            await update_ui(Texts.STARTING_DOWNLOAD, None)
            try:
                await bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
                )
            except Exception:
                pass

            file_path: Union[str, io.BytesIO, None] = None
            error: Optional[str] = None

            # Orchestrate TikTok fallbacks locally to decouple downloader
            if payload.format_id == "tikwm_fallback":
                file_path, error = await TikWMService.download_video(payload.page_url)

                # Smart BVC2/HEVC Fallback:
                # If TikWM gives us an incompatible proprietary byte stream (like BVC2),
                # ffmpeg cannot decode it, creating scrambled video. Instead of transcoding,
                # we immediately fallback to yt-dlp to fetch TikTok's native H.264 alternate stream.
                if file_path and not error:
                    meta = await _extract_video_meta(file_path)
                    vcodec = meta.get("vcodec")
                    pix_fmt = meta.get("pix_fmt")
                    codec_tag = meta.get("codec_tag", "")

                    is_safe_codec = (vcodec is not None) and (vcodec in _TG_SAFE_CODECS)
                    is_safe_pix_fmt = not pix_fmt or "10" not in pix_fmt
                    is_safe_tag = "bvc" not in codec_tag and "hvc" not in codec_tag

                    if not (is_safe_codec and is_safe_pix_fmt and is_safe_tag):
                        logger.warning(
                            "TikWM returned incompatible format (codec:%s, pix_fmt:%s, tag:%s). Falling back to yt-dlp H.264 stream...",
                            vcodec,
                            pix_fmt,
                            codec_tag,
                        )
                        safe_remove(file_path)
                        file_path, error = await MediaSender.download_video(
                            payload.page_url,
                            "bestvideo[vcodec^=avc]+bestaudio/best",
                            payload.height,
                            token + "_yt",
                        )

                if file_path and not error:
                    state.file_cache[token] = file_path
            elif payload.format_id == "gallerydl_fallback":
                file_path, error = await asyncio.to_thread(
                    GalleryDlService.download_video,
                    payload.page_url,
                    state.ytdlp.cookies_path,
                    state.ytdlp.tiktok_proxy,
                )
                if file_path and not error:
                    state.file_cache[token] = file_path
            elif payload.format_id == "pinterest_native":
                file_path, error = await PinterestNativeService.download_video(
                    payload.page_url
                )
                if file_path and not error:
                    state.file_cache[token] = file_path
            else:
                file_path, error = await MediaSender.download_video(
                    payload.page_url,
                    payload.format_id or "",
                    payload.height,
                    token,
                    info_json_path=payload.info_json_path,
                    fallback_clients=payload.youtube_fallback,
                )

            if error or not file_path:
                await update_ui(error or Texts.GENERIC_ERROR_SHORT, kb_error)
                return False

            await update_ui(Texts.SENDING_TO_TG, None)

            is_gif = payload.format_id == GIF_FORMAT_ID
            is_audio = payload.format_id == AUDIO_FORMAT_ID

            # Re-encode non-H.264 videos for Telegram compatibility
            # (TikTok CDN often serves HEVC which Telegram can't play)
            if isinstance(file_path, str) and not is_gif and not is_audio:
                file_path = await _ensure_telegram_compatible(file_path)

            # Extract video metadata for faster Telegram delivery + preview
            video_meta = await _extract_video_meta(
                file_path,
                info_json_path=payload.info_json_path,
            )

            success = await MediaSender.send_file(
                bot,
                chat_id,
                file_path,
                is_audio=is_audio,
                is_gif=is_gif,
                caption="📹" if not is_audio else "🎵",
                duration=video_meta.get("duration"),
                width=video_meta.get("width"),
                height=video_meta.get("height"),
            )

            if not success:
                await update_ui(Texts.SEND_ERROR, kb_error)
                return False

            return True
        finally:
            state.tasks_sem.release()

            # Cleanup info JSON after download (prevent /tmp fill)
            info_json = payload.info_json_path
            if info_json:
                await asyncio.to_thread(safe_remove, info_json)
