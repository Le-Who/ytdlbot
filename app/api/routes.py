import os
import secrets
import uuid
import asyncio
import time
import logging
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from telegram import Update

from app.core import state
from app.core.config import TELEGRAM_SECRET_TOKEN, TEMP_DIR
from app.core.utils import run_subprocess, safe_remove
from app.constants import CHUNK_SIZE, GIF_FORMAT_ID, AUDIO_FORMAT_ID

logger = logging.getLogger("app.api")
router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/dl/{token}")
async def download(token: str):
    logger.info(f"[DOWNLOAD] Token: {token}")
    payload = state.link_cache.get(token)
    if not payload:
        raise HTTPException(404, "Link expired")

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

    height = payload.get("height")
    # Determine if this format requires merge (video+audio streams).
    # Pipe streaming (output="-") cannot merge DASH video+audio.
    # If height is specified, builders.py creates a format selector like
    # "bestvideo[height=X]+(audio)", which requires merge.
    # Also "+" in format_id means explicit merge.
    needs_merge = bool(height) or ("+" in format_id)

    if is_gif:
        # GIF: download video → convert → stream
        async def stream_gif():
            tmp_dir = TEMP_DIR
            video_tmp = os.path.join(tmp_dir, f"ytdl_video_{uuid.uuid4().hex}.mp4")

            try:
                cmd = state.ytdlp.build_command(
                    payload["page_url"],
                    payload["format_id"],
                    payload.get("height"),
                    output=video_tmp,
                )
                logger.info(f"[STREAM-GIF] Download: {' '.join(cmd)}")

                async for proc, stderr in run_subprocess(cmd):
                    # Consume stdout to prevent deadlock
                    while await proc.stdout.read(4096):
                        pass

                    await proc.wait()
                    if proc.returncode != 0:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM-GIF] Download error: {err}")
                        return

                logger.info(f"[STREAM-GIF] Converting to GIF...")
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-i",
                    video_tmp,
                    "-vf",
                    "fps=10,scale=320:-1:flags=lanczos",
                    "-t",
                    "10",
                    "-y",
                    "-pix_fmt",
                    "rgb24",
                    "-f",
                    "gif",
                    "-",
                ]

                async for proc, stderr in run_subprocess(ffmpeg_cmd):
                    while True:
                        chunk = await proc.stdout.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
                    if proc.returncode != 0:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM-GIF] FFmpeg error: {err}")

            except Exception as e:
                logger.error(f"[STREAM-GIF] Exception: {e}", exc_info=True)
            finally:
                await asyncio.to_thread(safe_remove, video_tmp)

        return StreamingResponse(
            stream_gif(),
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
            },
        )

    elif needs_merge:
        # Formats that require merge (DASH video+audio) cannot be pipe-streamed.
        # Download to temp file first, then serve it.
        tmp_path = os.path.join(TEMP_DIR, f"ytdl_dl_{uuid.uuid4().hex}.{file_ext}")

        cmd = state.ytdlp.build_command(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            output=tmp_path,
            max_filesize=None,
        )
        logger.info(f"[DL-MERGE] {' '.join(cmd)}")

        try:
            async for proc, stderr in run_subprocess(cmd):
                while await proc.stdout.read(4096):
                    pass
                await proc.wait()
                if proc.returncode != 0:
                    err = b"".join(stderr).decode(errors="ignore")[-500:]
                    logger.error(f"[DL-MERGE] Error: {err}")
                    raise HTTPException(500, "Download failed")

            if not os.path.exists(tmp_path):
                raise HTTPException(500, "File not created")

            # Stream the file and clean up after
            async def stream_file():
                try:
                    with open(tmp_path, "rb") as f:
                        while True:
                            chunk = f.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    safe_remove(tmp_path)

            file_size = os.path.getsize(tmp_path)
            return StreamingResponse(
                stream_file(),
                media_type=media_type,
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}",
                    "Content-Length": str(file_size),
                },
            )

        except HTTPException:
            await asyncio.to_thread(safe_remove, tmp_path)
            raise
        except Exception as e:
            logger.error(f"[DL-MERGE] Exception: {e}", exc_info=True)
            await asyncio.to_thread(safe_remove, tmp_path)
            raise HTTPException(500, "Internal error")

    else:
        # Single-stream formats (audio, progressive) — pipe streaming works fine
        cmd = state.ytdlp.build_command(
            payload["page_url"],
            payload["format_id"],
            payload.get("height"),
            output="-",
        )
        logger.info(f"[STREAM] {' '.join(cmd)}")

        async def stream_pipe():
            try:
                async for proc, stderr in run_subprocess(cmd):
                    stream_timeout = 900
                    start_time = time.time()
                    loop = asyncio.get_running_loop()

                    chunk = None
                    while True:
                        if time.time() - start_time > stream_timeout:
                            logger.error("[STREAM] Overall timeout exceeded")
                            break

                        try:
                            async with asyncio.timeout(45.0) as cm:
                                while True:
                                    if time.time() - start_time > stream_timeout:
                                        break

                                    chunk = await proc.stdout.read(CHUNK_SIZE)
                                    if not chunk:
                                        break

                                    cm.reschedule(None)
                                    yield chunk
                                    cm.reschedule(loop.time() + 45.0)

                                if not chunk or (
                                    time.time() - start_time > stream_timeout
                                ):
                                    break
                        except TimeoutError:
                            logger.warning("[STREAM] Chunk read timeout, continuing...")
                            continue

                    if proc.returncode != 0 and proc.returncode is not None:
                        err = b"".join(stderr).decode(errors="ignore")[-500:]
                        logger.error(f"[STREAM] Error (code {proc.returncode}): {err}")

            except Exception as e:
                logger.error(f"[STREAM] Exception: {e}", exc_info=True)

        return StreamingResponse(
            stream_pipe(),
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
            },
        )


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Обработка вебхука от Telegram"""
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not token or not secrets.compare_digest(token, TELEGRAM_SECRET_TOKEN):
        raise HTTPException(401, "Unauthorized")

    if state.bot_app:
        try:
            update = Update.de_json(await request.json(), state.bot_app.bot)
            await state.bot_app.process_update(update)
        except Exception as e:
            logger.error(f"Webhook update error: {e}")
    return {"ok": True}
