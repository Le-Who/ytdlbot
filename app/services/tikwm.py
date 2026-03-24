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
import uuid
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import quote
import urllib.parse

from curl_cffi.requests import AsyncSession, Response
from app.core.config import TEMP_DIR
from app.core.utils import safe_remove

_tikwm_lock = asyncio.Lock()
_last_request_time = 0.0
_tikwm_cache: dict[str, tuple["TikWMResult", float]] = {}

__all__ = ["TikWMService"]

logger = logging.getLogger("app.services.tikwm")

API_BASE = "https://tikwm.com/api/"
TIMEOUT = 15  # seconds
MAX_RETRIES = 3
CHUNK_SIZE = 1024 * 1024  # 1 MB


@dataclass
class TikWMResult:
    status: str  # "video", "picker" (slideshow), or "error"
    title: str = ""
    url: Optional[str] = None  # video url
    images: Optional[List[str]] = None  # for slideshow
    audio_url: Optional[str] = None  # for slideshow
    error_message: Optional[str] = None

    @property
    def is_slideshow(self) -> bool:
        return self.status == "picker"


class TikWMService:
    """Fetches TikTok media via the TikWM API."""

    @staticmethod
    async def process(url: str) -> TikWMResult:
        """
        Fetch TikTok media info via TikWM API.
        Returns unified TikWMResult for Video or Picker (Slideshow).
        """
        # Unshorten vm.tiktok.com or vt.tiktok.com links before passing to TikWM
        if "vm.tiktok.com/" in url or "vt.tiktok.com/" in url:
            try:
                async with AsyncSession() as session:
                    head_resp = await session.head(
                        url, impersonate="chrome", timeout=10, allow_redirects=True
                    )
                    unshortened_url = str(head_resp.url)
                    # Often the unshortened URL has tracking query params.
                    # We can strip them to produce a cleaner URL for TikWM parsing.
                    if "?" in unshortened_url:
                        unshortened_url = unshortened_url.split("?")[0]
                    logger.info(
                        "[TIKWM] Unshortened URL %s -> %s", url, unshortened_url
                    )
                    url = unshortened_url
            except Exception as e:
                logger.warning("[TIKWM] Failed to unshorten URL %s: %s", url, e)

        api_url = f"{API_BASE}?url={quote(url, safe='')}&hd=1"
        last_error: Optional[str] = None
        global _last_request_time

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
            except Exception as e:
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

            # Check for slideshow (images)
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

            # Check for standard video
            video_url = info.get("hdplay") or info.get("play")
            if video_url:
                logger.info(
                    "[TIKWM] Got video URL (duration=%ss, title=%s)",
                    info.get("duration", "?"),
                    title[:60],
                )
                res = TikWMResult(status="video", title=title, url=video_url)

                # Apply 80% dynamic TTL cache based on `expire=` parameter
                ttl = 7200  # 2 hours default
                try:
                    parsed = urllib.parse.urlparse(video_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "expire" in qs:
                        expire_ts = int(qs["expire"][0])
                        remaining = expire_ts - time.time()
                        if remaining > 0:
                            ttl = remaining * 0.8
                except Exception as e:
                    logger.debug("[TIKWM] URL expiration parse error: %s", e)

                _tikwm_cache[url] = (res, time.time() + ttl)
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
        url: str, direct_video_url: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch video URL via TikWM API (or use direct url), then download to a temp file.

        Returns:
            (file_path, None) on success.
            (None, error_message) on failure.
        """
        if not direct_video_url:
            res = await TikWMService.process(url)
            if res.status != "video" or not res.url:
                return None, res.error_message or "TikWM: Not a video"
            video_url = res.url
        else:
            video_url = direct_video_url

        # Download the video from CDN
        output_path = os.path.join(TEMP_DIR, f"tikwm_{uuid.uuid4().hex}.mp4")

        try:
            assert video_url is not None  # guaranteed by fetch_video success path
            async with AsyncSession() as session:
                resp = await session.get(
                    video_url,
                    impersonate="chrome",
                    timeout=60,
                )

                def _write_file(path: str, data: bytes) -> None:
                    with open(path, "wb") as f:
                        f.write(data)

                await asyncio.to_thread(_write_file, output_path, resp.content)

            file_size = await asyncio.to_thread(os.path.getsize, output_path)
            size_mb = file_size / (1024 * 1024)
            logger.info("[TIKWM] Downloaded: %s (%.1f MB)", output_path, size_mb)
            return output_path, None

        except Exception as e:
            logger.error("[TIKWM] Download failed: %s", e)
            # Clean up partial file
            await asyncio.to_thread(safe_remove, output_path)
            return None, f"TikWM download error: {e}"

    @staticmethod
    async def download_audio(audio_url: str) -> Optional[str]:
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
                )

                def _write_file(path: str, data: bytes) -> None:
                    with open(path, "wb") as f:
                        f.write(data)

                await asyncio.to_thread(_write_file, output_path, resp.content)

            file_size = await asyncio.to_thread(os.path.getsize, output_path)
            size_mb = file_size / (1024 * 1024)
            logger.info("[TIKWM] Downloaded audio: %s (%.1f MB)", output_path, size_mb)
            return output_path
        except Exception as e:
            logger.error("[TIKWM] Audio download failed: %s", e)
            await asyncio.to_thread(safe_remove, output_path)
            return None

    @staticmethod
    async def download_slideshow(res: TikWMResult) -> tuple[List[str], Optional[str]]:
        """
        Download all images and the audio track from a TikWM picker response.
        Returns:
            (image_paths, audio_path)
        """
        if not res.images:
            return [], None

        image_paths = []
        try:
            async with AsyncSession() as session:

                def _write_file(path: str, data: bytes) -> None:
                    with open(path, "wb") as f:
                        f.write(data)

                for idx, img_url in enumerate(res.images):
                    out_path = os.path.join(
                        TEMP_DIR, f"tikwm_slide_{uuid.uuid4().hex}_{idx}.jpg"
                    )
                    resp = await session.get(img_url, impersonate="chrome", timeout=30)
                    await asyncio.to_thread(_write_file, out_path, resp.content)
                    image_paths.append(out_path)
        except Exception as e:
            logger.error("TikWM slideshow image download failed: %s", e)
            # Cleanup what we have
            for p in image_paths:
                await asyncio.to_thread(safe_remove, p)
            return [], None

        audio_path = None
        if res.audio_url:
            audio_path = await TikWMService.download_audio(res.audio_url)

        return image_paths, audio_path
