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

logger = logging.getLogger("app.services.orchestrator")


async def _extract_video_meta(
    file_path: str | object,
    info_json_path: Optional[str] = None,
) -> dict:
    """Extract duration/width/height for Telegram send_video.

    Strategy: try info JSON first (zero-cost), fall back to ffprobe.
    Returns dict with keys: duration, width, height (all Optional[int]).
    """
    meta: dict = {"duration": None, "width": None, "height": None}

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
    except Exception:
        pass

    return meta


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
