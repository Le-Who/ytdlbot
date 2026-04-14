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
    # The resolved (unshortened) page URL used as the _tikwm_cache key.
    # download_video() must use this — not the original short URL — for
    # correct cache eviction when the CDN URL turns out to be invalid.
    canonical_url: Optional[str] = None

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
        # Unshorten vm.tiktok.com or vt.tiktok.com links before passing to TikWM.
        # Keep the original short URL so we can also invalidate that cache key
        # if the caller needs to evict (short URL is what download_video() has).
        _original_url = url
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
            hdplay = info.get("hdplay")
            play = info.get("play")

            # Smart BVC2/HEVC Detection Heuristic:
            # BVC2/HEVC compresses significantly better than standard H.264.
            # If the "HD" stream is smaller than the "SD" stream, it is guaranteed to be a proprietary codec.
            size = info.get("size", 0)
            hd_size = info.get("hd_size", 0)

            if hdplay and play and hd_size > 0 and size > 0 and hd_size < size:
                logger.info(
                    "[TIKWM] Detecting HEVC/BVC2 because HD is smaller than SD (%.1fMB vs %.1fMB). Dropping to 'play' url (H.264).",
                    hd_size / 1048576,
                    size / 1048576,
                )
                video_url = play
            else:
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
                    canonical_url=url,  # url is already unshortened here
                )

                # Apply 80% dynamic TTL cache based on `expire=` parameter
                ttl = 7200  # 2 hours default
                try:
                    parsed = urllib.parse.urlparse(video_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "expire" in qs:
                        expire_ts = int(qs["expire"][0])
                        remaining = expire_ts - time.time()
                        if remaining > 0:
                            ttl = int(remaining * 0.8)
                except Exception as e:
                    logger.debug("[TIKWM] URL expiration parse error: %s", e)

                # Cache under both the canonical (long) URL and the original
                # short URL so that callers holding either form can evict.
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
        direct_video_url: Optional[str] = None,
        _cdn_retry: bool = True,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch video URL via TikWM API (or use direct url), then download to a temp file.

        Args:
            url: Original TikTok page URL (may be a short vt.tiktok.com link).
            direct_video_url: Skip the API call and use this CDN URL directly.
            _cdn_retry: Internal flag — when True, a single retry with a fresh
                API call is attempted after a CDN 404/empty-body failure.
                Set to False on the retry call to prevent infinite recursion.

        Returns:
            (file_path, None) on success.
            (None, error_message) on failure.
        """
        # Resolve video URL (from cache/API or direct override)
        _canonical: Optional[str] = None  # cache key for potential eviction
        if not direct_video_url:
            res = await TikWMService.process(url)
            if res.status != "video" or not res.url:
                return None, res.error_message or "TikWM: Not a video"
            video_url = res.url
            _canonical = res.canonical_url or url  # prefer the long-form key
        else:
            video_url = direct_video_url

        # Download the video from CDN
        output_path = os.path.join(TEMP_DIR, f"tikwm_{uuid.uuid4().hex}.mp4")

        # Minimum valid MP4 size — anything smaller is an error page / redirect body.
        _MIN_VIDEO_BYTES = 10 * 1024  # 10 KB

        try:
            assert video_url is not None  # guaranteed by fetch_video success path
            async with AsyncSession() as session:
                resp = await session.get(
                    video_url,
                    impersonate="chrome",
                    timeout=60,
                )

            # ── Guard: reject non-200 CDN responses ──────────────────────────
            if resp.status_code != 200:
                logger.error(
                    "[TIKWM] CDN returned HTTP %d for %s — evicting cache",
                    resp.status_code,
                    video_url[:120],
                )
                # Evict using the canonical (long-form) URL — that is the actual
                # cache key in _tikwm_cache.  Evicting with the short URL was a
                # silent no-op and caused infinite retries against a dead URL.
                for _key in {url, _canonical}:
                    if _key:
                        _tikwm_cache.pop(_key, None)
                if _cdn_retry:
                    logger.info("[TIKWM] CDN %d — retrying with fresh API fetch...", resp.status_code)
                    return await TikWMService.download_video(url, _cdn_retry=False)
                return None, f"TikWM CDN error: HTTP {resp.status_code}"

            content = resp.content

            # ── Guard: reject suspiciously small bodies (error JSON / HTML) ──
            if not content or len(content) < _MIN_VIDEO_BYTES:
                ct = resp.headers.get("content-type", "unknown")
                logger.error(
                    "[TIKWM] CDN body too small (%d bytes, content-type=%s) — likely an error page. Evicting cache.",
                    len(content) if content else 0,
                    ct,
                )
                for _key in {url, _canonical}:
                    if _key:
                        _tikwm_cache.pop(_key, None)
                if _cdn_retry:
                    logger.info("[TIKWM] CDN returned empty body — retrying with fresh API fetch...")
                    return await TikWMService.download_video(url, _cdn_retry=False)
                return None, "TikWM CDN returned empty/invalid body"

            def _write_file(path: str, data: bytes) -> None:
                with open(path, "wb") as f:
                    f.write(data)

            await asyncio.to_thread(_write_file, output_path, content)

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

        images = res.images  # bind to local for type narrowing
        image_paths: List[Optional[str]] = [None] * len(images)
        _dl_sem = asyncio.Semaphore(3)  # max 3 concurrent CDN fetches

        async def _download_one(idx: int, img_url: str) -> None:
            async with _dl_sem:
                out_path = os.path.join(
                    TEMP_DIR, f"tikwm_slide_{uuid.uuid4().hex}_{idx}.jpg"
                )
                try:
                    async with AsyncSession() as session:
                        resp = await session.get(
                            img_url, impersonate="chrome", timeout=30
                        )

                    # Validate HTTP response
                    if resp.status_code != 200:
                        logger.warning(
                            "[TIKWM] Image %d/%d returned HTTP %d: %s",
                            idx + 1,
                            len(images),
                            resp.status_code,
                            img_url[:120],
                        )
                        return

                    content = resp.content
                    if not content or len(content) < 100:
                        logger.warning(
                            "[TIKWM] Image %d/%d is too small (%d bytes), skipping",
                            idx + 1,
                            len(images),
                            len(content) if content else 0,
                        )
                        return

                    ct = resp.headers.get("content-type", "unknown")
                    logger.debug(
                        "[TIKWM] Image %d/%d: %d bytes, content-type=%s",
                        idx + 1,
                        len(images),
                        len(content),
                        ct,
                    )

                    def _write_file(path: str, data: bytes) -> None:
                        with open(path, "wb") as f:
                            f.write(data)

                    await asyncio.to_thread(_write_file, out_path, content)
                    image_paths[idx] = out_path
                except Exception as e:
                    logger.error(
                        "[TIKWM] Image %d/%d download error: %s",
                        idx + 1,
                        len(images),
                        e,
                    )

        try:
            await asyncio.gather(
                *[_download_one(i, url) for i, url in enumerate(images)]
            )

            # Filter out failed downloads (None), preserving order
            valid_paths = [p for p in image_paths if p is not None]

            logger.info(
                "[TIKWM] Downloaded %d/%d slideshow images",
                len(valid_paths),
                len(images),
            )
        except Exception as e:
            logger.error("TikWM slideshow image download failed: %s", e, exc_info=True)
            for p in image_paths:
                if p:
                    await asyncio.to_thread(safe_remove, p)
            return [], None

        audio_path = None
        if res.audio_url:
            audio_path = await TikWMService.download_audio(res.audio_url)

        return valid_paths, audio_path
