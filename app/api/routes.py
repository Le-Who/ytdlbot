import asyncio
import hmac
import logging
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from telegram import Update

from app.constants import AUDIO_FORMAT_ID, CHUNK_SIZE, GIF_FORMAT_ID
from app.core import state
from app.core.config import TELEGRAM_SECRET_TOKEN, DL_TIMEOUT_HTTP, MAX_DL_MB
from app.core.logging import set_correlation_id
from app.core.process import run_subprocess

logger = logging.getLogger("app.api")
router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/metrics")
async def metrics_endpoint():
    from app.core.metrics import metrics
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/dl/{token}")
async def download(token: str, request: Request):
    payload = state.link_cache.get(token)
    if not payload:
        raise HTTPException(404, "Link expired")

    host = urlsplit(payload.get("page_url", "")).hostname or "unknown"
    set_correlation_id(token)
    logger.info(
        "download request", extra={"op": "download", "token": token, "url_host": host}
    )

    ip = request.client.host if request.client else "unknown"
    if not state.limiter.allow_ip(ip) or not state.limiter.allow_token(token):
        raise HTTPException(429, "Too many requests")

    raw_title = payload.get("title") or "video"
    clean_title = re.sub(r'[\x00-\x1f\x7f\r\n]', '', raw_title)[:200]
    encoded_filename = quote(clean_title or "video")
    format_id = payload.get("format_id")
    is_gif = format_id == GIF_FORMAT_ID
    is_audio = format_id == AUDIO_FORMAT_ID

    if is_audio:
        file_ext = "mp3"
        media_type = "audio/mpeg"
    else:
        # Both video and GIF (mute MP4) use mp4
        file_ext = "mp4"
        media_type = "video/mp4"

    max_bytes = MAX_DL_MB * 1024 * 1024

    async def stream_video_subprocess():
        bytes_sent = 0
        if is_gif:
            # GIF = download video directly to ffmpeg pipe
            cmd = state.ytdlp.build_command(
                payload["page_url"],
                payload["format_id"],
                payload.get("height"),
                output="-",
            )
            # Mute MP4: strip audio, copy video stream (no quality loss, instant)
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                "pipe:0",
                "-c:v",
                "copy",
                "-an",
                "-t",
                "60",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-f",
                "mp4",
                "-",
            ]
            async with run_subprocess(cmd, timeout=DL_TIMEOUT_HTTP) as dl_handle:
                async with run_subprocess(
                    ffmpeg_cmd, stdin=asyncio.subprocess.PIPE, timeout=300
                ) as ff_handle:

                    async def read_ytdlp_write_ffmpeg():
                        try:
                            while True:
                                chunk = await dl_handle.proc.stdout.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                ff_handle.proc.stdin.write(chunk)
                                await ff_handle.proc.stdin.drain()
                        except Exception as e:
                            logger.debug(f"Pipe stream error: {e}")
                        finally:
                            try:
                                ff_handle.proc.stdin.close()
                            except Exception:
                                pass

                    pipe_task = asyncio.create_task(read_ytdlp_write_ffmpeg())
                    try:
                        while True:
                            chunk = await ff_handle.proc.stdout.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            bytes_sent += len(chunk)
                            if bytes_sent > max_bytes:
                                logger.warning(f"Stream exceeded {MAX_DL_MB}MB limit, terminating")
                                return
                            yield chunk
                    finally:
                        pipe_task.cancel()
        else:
            cmd = state.ytdlp.build_command(
                payload["page_url"],
                payload["format_id"],
                payload.get("height"),
                output="-",
            )
            async with run_subprocess(cmd, timeout=DL_TIMEOUT_HTTP) as handle:
                proc = handle.proc
                while True:
                    chunk = await proc.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    if bytes_sent > max_bytes:
                        logger.warning(f"Stream exceeded {MAX_DL_MB}MB limit, terminating")
                        return
                    yield chunk

    return StreamingResponse(
        stream_video_subprocess(),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
        },
    )


@router.post("/webhook")
async def telegram_webhook(request: Request):
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not token or not hmac.compare_digest(token, TELEGRAM_SECRET_TOKEN):
        raise HTTPException(401, "Unauthorized")

    # Rate limit by source IP to prevent replay attacks
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    if ip and not state.limiter.allow_ip(ip):
        raise HTTPException(429, "Too many requests")

    if state.bot_app:
        try:
            update = Update.de_json(await request.json(), state.bot_app.bot)
            await state.bot_app.process_update(update)
        except Exception as e:
            logger.error(f"Webhook update error: {e}")
    return {"ok": True}
