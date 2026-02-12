import asyncio
import hmac
import logging
import os
import time
import uuid
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from telegram import Update

from app.constants import AUDIO_FORMAT_ID, CHUNK_SIZE, GIF_FORMAT_ID
from app.core import state
from app.core.config import TELEGRAM_SECRET_TOKEN, TEMP_DIR
from app.core.logging import set_correlation_id
from app.core.process import run_subprocess
from app.core.utils import safe_remove

logger = logging.getLogger("app.api")
router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/dl/{token}")
async def download(token: str, request: Request):
    payload = state.link_cache.get(token)
    if not payload:
        raise HTTPException(404, "Link expired")

    host = urlsplit(payload.get("page_url", "")).hostname or "unknown"
    set_correlation_id(token)
    logger.info("download request", extra={"op": "download", "token": token, "url_host": host})

    ip = request.client.host if request.client else "unknown"
    if not state.limiter.allow_ip(ip) or not state.limiter.allow_token(token):
        raise HTTPException(429, "Too many requests")

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
            video_tmp = os.path.join(TEMP_DIR, f"ytdl_video_{uuid.uuid4().hex}.mp4")
            try:
                cmd = state.ytdlp.build_command(payload["page_url"], payload["format_id"], payload.get("height"), output=video_tmp)
                async with run_subprocess(cmd, timeout=900) as handle:
                    proc = handle.proc
                    while await proc.stdout.read(4096):
                        pass
                    await proc.wait()
                    if proc.returncode != 0:
                        logger.error("gif download failed", extra={"op": "gif-download", "error_type": "SubprocessError"})
                        return

                ffmpeg_cmd = [
                    "ffmpeg", "-i", video_tmp,
                    "-vf", "fps=10,scale=320:-1:flags=lanczos",
                    "-t", "10", "-y", "-pix_fmt", "rgb24", "-f", "gif", "-",
                ]
                async with run_subprocess(ffmpeg_cmd, timeout=300) as handle:
                    proc = handle.proc
                    while True:
                        chunk = await proc.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                await asyncio.to_thread(safe_remove, video_tmp)
        else:
            cmd = state.ytdlp.build_command(payload["page_url"], payload["format_id"], payload.get("height"), output="-")
            start_time = time.time()
            async with run_subprocess(cmd, timeout=900) as handle:
                proc = handle.proc
                while True:
                    if time.time() - start_time > 900:
                        break
                    chunk = await proc.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

    return StreamingResponse(
        stream_video_subprocess(),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"},
    )


@router.post("/webhook")
async def telegram_webhook(request: Request):
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not token or not hmac.compare_digest(token, TELEGRAM_SECRET_TOKEN):
        raise HTTPException(401, "Unauthorized")

    if state.bot_app:
        try:
            update = Update.de_json(await request.json(), state.bot_app.bot)
            await state.bot_app.process_update(update)
        except Exception as e:
            logger.error(f"Webhook update error: {e}")
    return {"ok": True}
