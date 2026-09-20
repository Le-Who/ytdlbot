import asyncio
import hmac
import logging
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from telegram import Update

from app.constants import AUDIO_FORMAT_ID, CHUNK_SIZE, GIF_FORMAT_ID
from app.core import state
from app.core.config import DL_TIMEOUT_HTTP, MAX_DL_MB, TELEGRAM_SECRET_TOKEN
from app.core.logging import set_correlation_id
from app.core.models import DownloadContext
from app.core.policy import max_media_file_bytes
from app.core.process import run_subprocess

logger = logging.getLogger("app.api")
router = APIRouter()
_HTTP_LEASE_RENEW_SECONDS = 5 * 60


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/metrics")
async def metrics_endpoint():
    from app.core.metrics import metrics

    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/dl/{token}")
async def download(token: str, request: Request):  # type: ignore[no-untyped-def]
    payload = await state.link_cache.get(token)
    if not payload:
        raise HTTPException(404, "Link expired")

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    host = urlsplit(payload.page_url).hostname or "unknown"
    set_correlation_id(token)
    logger.info(
        "download request", extra={"op": "download", "token": token, "url_host": host}
    )

    ip = request.client.host if request.client else "unknown"
    if not await state.limiter.allow_ip(ip) or not await state.limiter.allow_token(
        token
    ):
        raise HTTPException(429, "Too many requests")

    raw_title = payload.title or "video"
    clean_title = re.sub(r"[\x00-\x1f\x7f\r\n]", "", raw_title)[:200]
    encoded_filename = quote(clean_title or "video")
    format_id = payload.format_id
    is_gif = format_id == GIF_FORMAT_ID
    is_audio = format_id == AUDIO_FORMAT_ID

    if is_audio:
        file_ext = "mp3"
        media_type = "audio/mpeg"
    else:
        # Both video and GIF (mute MP4) use mp4
        file_ext = "mp4"
        media_type = "video/mp4"

    max_bytes = max_media_file_bytes(limit_mb=MAX_DL_MB)

    if state.media_pipeline is not None:
        from app.services.media.pipeline import (
            MediaPipelineError,
            request_from_download_context,
        )

        media_request = request_from_download_context(payload, caller_scope="api")
        lease = state.media_pipeline.open_materialized(media_request)
        try:
            materialized = await lease.__aenter__()
        except MediaPipelineError as error:
            raise HTTPException(502, str(error)) from error
        if len(materialized.paths) != 1:
            await lease.__aexit__(None, None, None)
            raise HTTPException(409, "HTTP download requires one media item")
        if materialized.size_bytes > max_bytes:
            await _exit_materialized(lease)
            raise HTTPException(413, "Media exceeds HTTP download size limit")
        media_path = materialized.paths[0]
        suffix = media_path.suffix.lower()
        media_type = {
            ".gif": "image/gif",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
        }.get(suffix, "application/octet-stream")
        file_ext = suffix.removeprefix(".") or "bin"

        async def stream_materialized():
            bytes_sent = 0
            renewal = asyncio.create_task(_renew_materialized_lease(materialized))
            active_error: BaseException | None = None
            try:
                with media_path.open("rb") as source:
                    while True:
                        _raise_lease_renewal_failure(renewal)
                        chunk = await asyncio.to_thread(source.read, CHUNK_SIZE)
                        _raise_lease_renewal_failure(renewal)
                        if not chunk:
                            break
                        bytes_sent += len(chunk)
                        if bytes_sent > max_bytes:
                            logger.warning(
                                "Stream exceeded size limit",
                                extra={"limit_mb": MAX_DL_MB},
                            )
                            return
                        yield chunk
            except BaseException as error:
                active_error = error
                raise
            finally:
                renewal_error = await _stop_lease_renewal(renewal)
                await _exit_materialized(lease)
                if renewal_error is not None:
                    if active_error is None:
                        raise renewal_error
                    logger.error(
                        "Media lease renewal failed during stream shutdown",
                        extra={"error_type": type(renewal_error).__name__},
                    )

        return StreamingResponse(
            stream_materialized(),
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
                )
            },
        )

    async def stream_video_subprocess():
        bytes_sent = 0
        if is_gif:
            # GIF = download video directly to ffmpeg pipe
            cmd = state.ytdlp.build_command(
                payload.page_url,
                payload.format_id,
                payload.height,
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
                                chunk = await dl_handle.proc.stdout.read(CHUNK_SIZE)  # type: ignore[union-attr]
                                if not chunk:
                                    break
                                ff_handle.proc.stdin.write(chunk)  # type: ignore[union-attr]
                                await ff_handle.proc.stdin.drain()  # type: ignore[union-attr]
                        except Exception as e:
                            logger.debug("Pipe stream error", extra={"error": str(e)})
                        finally:
                            try:
                                ff_handle.proc.stdin.close()  # type: ignore[union-attr]
                            except Exception:
                                pass

                    pipe_task = asyncio.create_task(read_ytdlp_write_ffmpeg())
                    try:
                        while True:
                            chunk = await ff_handle.proc.stdout.read(CHUNK_SIZE)  # type: ignore[union-attr]
                            if not chunk:
                                break
                            bytes_sent += len(chunk)
                            if bytes_sent > max_bytes:
                                logger.warning(
                                    "Stream exceeded size limit",
                                    extra={"limit_mb": MAX_DL_MB},
                                )
                                return
                            yield chunk
                    finally:
                        pipe_task.cancel()
        else:
            cmd = state.ytdlp.build_command(
                payload.page_url,
                payload.format_id,
                payload.height,
                output="-",
            )
            async with run_subprocess(cmd, timeout=DL_TIMEOUT_HTTP) as handle:
                proc = handle.proc
                while True:
                    chunk = await proc.stdout.read(CHUNK_SIZE)  # type: ignore[union-attr]
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    if bytes_sent > max_bytes:
                        logger.warning(
                            "Stream exceeded size limit", extra={"limit_mb": MAX_DL_MB}
                        )
                        return
                    yield chunk

    return StreamingResponse(
        stream_video_subprocess(),
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}.{file_ext}"
        },
    )


async def _exit_materialized(lease) -> None:  # type: ignore[no-untyped-def]
    """Await lease cleanup even when the streaming response is cancelled."""
    cleanup = asyncio.create_task(lease.__aexit__(None, None, None))
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as error:
            cancellation = error
    if cancellation is not None:
        if not cleanup.cancelled():
            cleanup.exception()
        raise cancellation
    await cleanup


async def _renew_materialized_lease(materialized) -> None:  # type: ignore[no-untyped-def]
    """Keep local bytes protected while client backpressure suspends streaming."""
    while True:
        materialized.renew_lease()
        await asyncio.sleep(_HTTP_LEASE_RENEW_SECONDS)


def _raise_lease_renewal_failure(task: asyncio.Task[None]) -> None:
    if task.done() and not task.cancelled():
        task.result()


async def _stop_lease_renewal(
    task: asyncio.Task[None],
) -> BaseException | None:
    cancelled_here = not task.done()
    if cancelled_here:
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if not cancelled_here:
            raise
    except BaseException as error:
        return error
    return None


@router.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not token or not hmac.compare_digest(token, TELEGRAM_SECRET_TOKEN):
        raise HTTPException(401, "Unauthorized")

    # Rate limit by source IP to prevent replay attacks
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    if ip and not await state.limiter.allow_ip(ip):
        raise HTTPException(429, "Too many requests")

    if state.bot_app:
        try:
            update = Update.de_json(await request.json(), state.bot_app.bot)
            # Fire-and-forget: return 200 to Telegram immediately so it
            # keeps delivering updates for other users while this one
            # is being processed in the background.
            asyncio.create_task(state.bot_app.process_update(update))
        except Exception as e:
            logger.error("Webhook update error", extra={"error": str(e)})
    return {"ok": True}
