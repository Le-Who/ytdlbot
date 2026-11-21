"""
TikWM API service — third-party fallback for downloading TikTok videos.

Used when yt-dlp and gallery-dl both fail on age-restricted/classified content.
TikWM returns direct CDN video URLs regardless of datacenter IP restrictions.

API: https://tikwm.com/api/?url=<tiktok_url>&hd=1
Free tier: 5000 requests/day, 1 request/second.
"""

import asyncio
import logging
import os
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from curl_cffi.requests import AsyncSession, Response

from app.core.asyncio_utils import await_owned
from app.core.config import TEMP_DIR
from app.core.utils import safe_remove
from app.services.media.transport import redact_url

_tikwm_lock = asyncio.Lock()
_last_request_time = 0.0
_tikwm_cache: dict[str, tuple["TikWMResult", float]] = {}

__all__ = ["TikWMService"]

logger = logging.getLogger("app.services.tikwm")

API_BASE = "https://tikwm.com/api/"
TIMEOUT = 15
MAX_RETRIES = 3
CHUNK_SIZE = 1024 * 1024
MAX_MEDIA_BYTES = 2_000_000_000
MIN_VIDEO_BYTES = 10 * 1024


class _InvalidBodyLength(ValueError):
    pass


async def _stream_response(response: Response, output_path: str) -> int:
    """Stream a response to disk without ever materializing the whole body."""
    written = 0
    output = open(output_path, "wb")  # noqa: ASYNC230, SIM115
    try:
        try:
            async for chunk in response.aiter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_MEDIA_BYTES:
                    raise ValueError("media exceeds 2,000,000,000 byte limit")
                await await_owned(asyncio.to_thread(output.write, chunk))
        finally:
            await await_owned(asyncio.to_thread(output.close))
        declared = response.headers.get("content-length")
        if isinstance(declared, (str, int)):
            try:
                expected = int(declared)
            except ValueError:
                expected = -1
            if expected >= 0 and written != expected:
                raise _InvalidBodyLength("truncated media response")
        return written
    except BaseException:
        safe_remove(output_path)
        raise


@dataclass
class TikWMResult:
    status: str
    title: str = ""
    url: str | None = None
    images: list[str] | None = None
    audio_url: str | None = None
    error_message: str | None = None
    canonical_url: str | None = None

    @property
    def is_slideshow(self) -> bool:
        return self.status == "picker"


def _evict_failed_video(url: str, canonical_url: str | None, video_url: str) -> None:
    for key in (url, canonical_url):
        if (
            key is not None
            and (cached := _tikwm_cache.get(key)) is not None
            and cached[0].url == video_url
        ):
            del _tikwm_cache[key]


class TikWMService:
    """Fetches TikTok media via the TikWM API."""

    @staticmethod
    async def process(
        url: str, *, _rejected_video_url: str | None = None
    ) -> TikWMResult:
        """
        Fetch TikTok media info via TikWM API.
        Returns unified TikWMResult for Video or Picker (Slideshow).
        """
        _original_url = url
        if "vm.tiktok.com/" in url or "vt.tiktok.com/" in url:
            try:
                async with AsyncSession() as session:
                    head_resp = await session.head(
                        url, impersonate="chrome", timeout=10, allow_redirects=True
                    )
                    unshortened_url = str(head_resp.url)
                    if "?" in unshortened_url:
                        unshortened_url = unshortened_url.split("?")[0]
                    logger.info(
                        "[TIKWM] Unshortened URL %s -> %s", url, unshortened_url
                    )
                    url = unshortened_url
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[TIKWM] Failed to unshorten URL %s (%s)",
                    redact_url(url),
                    type(e).__name__,
                )

        api_url = f"{API_BASE}?url={quote(url, safe='')}&hd=1"
        last_error: str | None = None
        global _last_request_time

        if _rejected_video_url is not None:
            _evict_failed_video(_original_url, url, _rejected_video_url)

        now_ts = time.time()
        if url in _tikwm_cache:
            res, exp = _tikwm_cache[url]
            if now_ts < exp:
                logger.info(
                    "[TIKWM] CACHE HIT for %s (expires in %ds)", url, int(exp - now_ts)
                )
                return res
            else:
                del _tikwm_cache[url]

        for attempt in range(MAX_RETRIES):
            async with _tikwm_lock:
                now = time.time()
                elapsed = now - _last_request_time
                if elapsed < 1.1:
                    await asyncio.sleep(1.1 - elapsed)
                _last_request_time = time.time()

            try:
                async with AsyncSession() as session:
                    resp: Response = await session.get(
                        api_url,
                        impersonate="chrome",
                        timeout=TIMEOUT,
                    )
                    data = resp.json()
            except Exception as e:  # noqa: BLE001
                last_error = f"TikWM API error: {e}"
                logger.warning(
                    "[TIKWM] Attempt %d/%d failed: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    e,
                )
                continue

            code = data.get("code")
            if code == -1:
                msg = data.get("msg", "unknown error")
                if "limit" in msg.lower() or "too many" in msg.lower():
                    logger.warning(
                        "[TIKWM] Rate limited (code=-1, msg=%s). Retrying...", msg
                    )
                    await asyncio.sleep(2)
                    continue
                else:
                    logger.warning("[TIKWM] API returned error code=-1: %s", msg)
                    return TikWMResult(status="error", error_message=f"TikWM: {msg}")

            if code != 0:
                msg = data.get("msg", "unknown error")
                logger.warning("[TIKWM] API returned code=%s: %s", code, msg)
                return TikWMResult(status="error", error_message=f"TikWM: {msg}")

            info = data.get("data", {})
            raw_title = info.get("title", "TikTok Media")
            title = raw_title[:45].strip() + ("..." if len(raw_title) > 45 else "")

            images = info.get("images")
            if images and isinstance(images, list) and len(images) > 0:
                music_url = info.get("music")
                logger.info(
                    "[TIKWM] Got slideshow (%d images, title=%s)",
                    len(images),
                    title[:60],
                )
                return TikWMResult(
                    status="picker",
                    title=title,
                    images=images,
                    audio_url=music_url,
                )

            hdplay = info.get("hdplay")
            play = info.get("play")

            video_url = hdplay or play

            if video_url:
                logger.info(
                    "[TIKWM] Got video URL (duration=%ss, title=%s)",
                    info.get("duration", "?"),
                    title[:60],
                )
                res = TikWMResult(
                    status="video",
                    title=title,
                    url=video_url,
                    canonical_url=url,
                )

                ttl = 7200
                try:
                    parsed = urllib.parse.urlparse(video_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "expire" in qs:
                        expire_ts = int(qs["expire"][0])
                        remaining = expire_ts - time.time()
                        if remaining > 0:
                            ttl = int(remaining * 0.8)
                except (ValueError, TypeError, IndexError, OverflowError) as e:
                    logger.debug("[TIKWM] URL expiration parse error: %s", e)

                _tikwm_cache[url] = (res, time.time() + ttl)
                if _original_url != url:
                    _tikwm_cache[_original_url] = (res, time.time() + ttl)
                return res

            logger.warning("[TIKWM] Unrecognized response format (no video or images)")
            return TikWMResult(
                status="error", error_message="TikWM: No video or images in response"
            )

        return TikWMResult(
            status="error", error_message=last_error or "TikWM: max retries exceeded"
        )

    @staticmethod
    async def download_video(
        url: str,
        direct_video_url: str | None = None,
        _cdn_retry: bool = True,
    ) -> tuple[str | None, str | None]:
        """
        Fetch video URL via TikWM API (or use direct url), then download to a temp file.

        Args:
            url: Original TikTok page URL (may be a short vt.tiktok.com link).
            direct_video_url: Skip the API call and use this CDN URL directly.
            _cdn_retry: Allow one retry with fresh metadata after an HTTP or
                invalid-body failure.

        Returns:
            (file_path, None) on success.
            (None, error_message) on failure.
        """
        rejected_video_url: str | None = None
        last_error = "TikWM download error: Download failed"
        for attempt in range(2 if _cdn_retry else 1):
            if attempt == 0 and direct_video_url:
                video_url = direct_video_url
                cached = _tikwm_cache.get(url)
                canonical_url = (
                    cached[0].canonical_url
                    if cached is not None and cached[0].url == video_url
                    else None
                )
            else:
                res = (
                    await TikWMService.process(url)
                    if rejected_video_url is None
                    else await TikWMService.process(
                        url, _rejected_video_url=rejected_video_url
                    )
                )
                if res.status != "video" or not res.url:
                    return None, res.error_message or "TikWM: Not a video"
                video_url = res.url
                canonical_url = res.canonical_url

            output_path = os.path.join(TEMP_DIR, f"tikwm_{uuid.uuid4().hex}.mp4")
            try:
                content_size: int | None = None
                async with AsyncSession() as session:
                    resp = await session.get(
                        video_url,
                        impersonate="chrome",
                        timeout=60,
                        stream=True,
                    )
                    if resp.status_code == 200:
                        try:
                            content_size = await _stream_response(resp, output_path)
                        except _InvalidBodyLength:
                            pass

                if resp.status_code != 200:
                    last_error = f"TikWM CDN error: HTTP {resp.status_code}"
                    logger.error(
                        "[TIKWM] CDN returned HTTP %d for %s",
                        resp.status_code,
                        redact_url(video_url),
                    )
                elif content_size is None or content_size < MIN_VIDEO_BYTES:
                    last_error = "TikWM CDN returned empty/invalid body"
                    logger.error(
                        "[TIKWM] CDN returned an invalid video body for %s",
                        redact_url(video_url),
                    )
                    await await_owned(asyncio.to_thread(safe_remove, output_path))
                else:
                    size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    logger.info(
                        "[TIKWM] Downloaded: %s (%.1f MB)", output_path, size_mb
                    )
                    return output_path, None
            except asyncio.CancelledError:
                safe_remove(output_path)
                raise
            except Exception as error:  # noqa: BLE001
                logger.error("[TIKWM] Download failed (%s)", type(error).__name__)
                await await_owned(asyncio.to_thread(safe_remove, output_path))
                return None, "TikWM download error: Download failed"

            _evict_failed_video(url, canonical_url, video_url)
            rejected_video_url = video_url
        return None, last_error

    @staticmethod
    async def download_audio(audio_url: str) -> str | None:
        """
        Download the audio file for a slideshow.
        Returns the path to the downloaded mp3/m4a, or None on failure.
        """
        if not audio_url:
            return None

        output_path = os.path.join(TEMP_DIR, f"tikwm_audio_{uuid.uuid4().hex}.mp3")
        try:
            async with AsyncSession() as session:
                resp = await session.get(
                    audio_url,
                    impersonate="chrome",
                    timeout=60,
                    stream=True,
                )
                if resp.status_code != 200:
                    return None
                await _stream_response(resp, output_path)

            file_size = os.path.getsize(output_path)
            size_mb = file_size / (1024 * 1024)
            logger.info("[TIKWM] Downloaded audio: %s (%.1f MB)", output_path, size_mb)
            return output_path
        except asyncio.CancelledError:
            safe_remove(output_path)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[TIKWM] Audio download failed for %s (%s)",
                redact_url(audio_url),
                type(e).__name__,
            )
            await await_owned(asyncio.to_thread(safe_remove, output_path))
            return None

    @staticmethod
    async def download_slideshow(res: TikWMResult) -> tuple[list[str], str | None]:
        """
        Download all images and the audio track from a TikWM picker response.
        Returns:
            (image_paths, audio_path)
        """
        if not res.images:
            return [], None

        images = res.images
        image_paths: list[str | None] = [None] * len(images)
        _dl_sem = asyncio.Semaphore(3)

        async def _download_one(idx: int, img_url: str) -> None:
            async with _dl_sem:
                out_path = os.path.join(
                    TEMP_DIR, f"tikwm_slide_{uuid.uuid4().hex}_{idx}.jpg"
                )
                try:
                    async with AsyncSession() as session:
                        resp = await session.get(
                            img_url, impersonate="chrome", timeout=30, stream=True
                        )
                        if resp.status_code != 200:
                            logger.warning(
                                "[TIKWM] Image %d/%d returned HTTP %d: %s",
                                idx + 1,
                                len(images),
                                resp.status_code,
                                redact_url(img_url),
                            )
                            return

                        content_size = await _stream_response(resp, out_path)
                    if content_size < 100:
                        logger.warning(
                            "[TIKWM] Image %d/%d is too small (%d bytes), skipping",
                            idx + 1,
                            len(images),
                            content_size,
                        )
                        await await_owned(asyncio.to_thread(safe_remove, out_path))
                        return

                    ct = resp.headers.get("content-type", "unknown")
                    logger.debug(
                        "[TIKWM] Image %d/%d: %d bytes, content-type=%s",
                        idx + 1,
                        len(images),
                        content_size,
                        ct,
                    )
                    image_paths[idx] = out_path
                except asyncio.CancelledError:
                    safe_remove(out_path)
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "[TIKWM] Image %d/%d download error (%s)",
                        idx + 1,
                        len(images),
                        type(e).__name__,
                    )
                    await await_owned(asyncio.to_thread(safe_remove, out_path))

        published = False
        try:
            async with asyncio.TaskGroup() as group:
                for i, url in enumerate(images):
                    group.create_task(_download_one(i, url))

            valid_paths = [p for p in image_paths if p is not None]

            logger.info(
                "[TIKWM] Downloaded %d/%d slideshow images",
                len(valid_paths),
                len(images),
            )
            audio_path = None
            if res.audio_url:
                audio_path = await TikWMService.download_audio(res.audio_url)
            published = True
            return valid_paths, audio_path
        except Exception as e:  # noqa: BLE001
            logger.error("TikWM slideshow image download failed (%s)", type(e).__name__)
            return [], None
        finally:
            if not published:
                for path in image_paths:
                    if path:
                        safe_remove(path)
