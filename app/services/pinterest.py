"""
Pinterest Native Service — v2 (Streaming + Carousel Support)

Extracts direct MP4 media links from Pinterest URLs using open graph meta tags,
completely bypassing heavy yt-dlp overhead and 403 blocks.

Changes from v1:
- Chunked async streaming download (no full-buffer memory bloat)
- Cobalt carousel fallback properly handles `picker` status
- Image extraction for carousels (og:image) for non-video pins
"""

import os
import uuid
import logging
import asyncio
from typing import Optional, Tuple, List

from curl_cffi.requests import AsyncSession
import re

from app.core.config import TEMP_DIR
from app.core.utils import safe_remove

logger = logging.getLogger("app.services.pinterest")

CHUNK_SIZE = 512 * 1024  # 512 KB chunks for streaming


class PinterestNativeService:
    @staticmethod
    async def extract_media_url(url: str) -> Tuple[Optional[str], Optional[str]]:
        """Scrape the og:video or og:image using a fast curl_cffi GET request.

        Returns:
            (video_url, image_url) — at least one may be set.
        """
        try:
            async with AsyncSession() as session:
                resp = await session.get(
                    url, impersonate="chrome", timeout=10, allow_redirects=True
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "[PINTEREST] Native scrape got HTTP %d", resp.status_code
                    )
                    return None, None

                html = resp.text

                # 1. Try og:video first (MP4 video pins)
                video_url = _extract_og_video(html)

                # 2. Try og:image (static image pins / carousels)
                image_url = _extract_og_image(html)

                # 3. Upgrade jpg to gif if the original animated asset exists
                if image_url:
                    match = re.search(
                        r"pinimg\.com/[^/]+/(.+)\.jpg", image_url, re.IGNORECASE
                    )
                    if match:
                        core_path = match.group(1)
                        expected_gif_url = (
                            f"https://i.pinimg.com/originals/{core_path}.gif"
                        )
                        if expected_gif_url in html:
                            image_url = expected_gif_url
                        else:
                            gif_match = re.search(
                                rf"(https://i\.pinimg\.com/[^/]+/{re.escape(core_path)}\.gif)",
                                html,
                                re.IGNORECASE,
                            )
                            if gif_match:
                                image_url = gif_match.group(1)

                if video_url:
                    logger.info("[PINTEREST] Found og:video: %s", video_url[:80])
                elif image_url and image_url.endswith(".gif"):
                    logger.info("[PINTEREST] Found original gif: %s", image_url[:80])
                elif image_url:
                    logger.info("[PINTEREST] Found og:image: %s", image_url[:80])
                else:
                    logger.warning(
                        "[PINTEREST] Native scrape found no og:video or og:image"
                    )

                return video_url, image_url

        except Exception as e:
            logger.error("[PINTEREST] Native extraction error: %s", e)
            return None, None

    @staticmethod
    async def download_video(url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract direct MP4 and download it via chunked streaming.
        Tier 1: Native OG Scraper (video)
        Tier 2: Native OG Scraper (single image)
        Tier 3: Cobalt fallback (video or carousel)
        """
        video_url, image_url = await PinterestNativeService.extract_media_url(url)

        # Tier 1: Direct video download with streaming
        if video_url:
            result = await _stream_download(video_url, "pin_", "mp4")
            if result:
                return result, None

        # Tier 2: Single image download
        if image_url:
            ext = "jpg"
            if ".gif" in image_url.lower():
                ext = "gif"
            elif ".png" in image_url.lower():
                ext = "png"
            result = await _stream_download(image_url, "pin_img_", ext)
            if result:
                return result, None

        # Tier 3: Cobalt fallback (handles both video and carousels)
        return await _cobalt_fallback(url)

    @staticmethod
    async def download_carousel(url: str) -> Tuple[Optional[List[str]], Optional[str]]:
        """
        Attempt to download a Pinterest carousel via Cobalt's picker response.
        Returns (list_of_image_paths, error_message) or (None, error).
        """
        from app.services.cobalt import CobaltService

        logger.info("[PINTEREST] Attempting carousel download via Cobalt: %s", url)
        c_res = await CobaltService.process(url)

        if c_res.status == "picker" and c_res.picker:
            # Download all picker items concurrently
            image_urls = [p.url for p in c_res.picker if p.url]
            if not image_urls:
                return None, "Cobalt picker returned empty items"

            paths: List[Optional[str]] = [None] * len(image_urls)
            sem = asyncio.Semaphore(4)

            async def _dl_one(idx: int, img_url: str) -> None:
                async with sem:
                    p = await _stream_download(img_url, f"pin_carousel_{idx}_", "jpg")
                    if p:
                        paths[idx] = p

            await asyncio.gather(*[_dl_one(i, u) for i, u in enumerate(image_urls)])
            valid = [p for p in paths if p is not None]
            if valid:
                logger.info(
                    "[PINTEREST] Carousel: downloaded %d/%d images",
                    len(valid),
                    len(image_urls),
                )
                return valid, None
            return None, "Failed to download carousel images"

        return None, "Not a carousel or Cobalt failed"


# ── Private helpers ──────────────────────────────────────────────────────────


def _extract_og_video(html: str) -> Optional[str]:
    """Extract og:video:secure_url or og:video from HTML."""
    match = re.search(
        r'<meta[^>]+property=["\']og:video(:secure_url)?["\'][^>]+content=["\']([^"\']+\.mp4(?:[?#][^"\']*)?)["\']',
        html,
        re.IGNORECASE,
    )
    if not match:
        # Alternative: content attribute comes first
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']+\.mp4(?:[?#][^"\']*)?)["\'][^>]+property=["\']og:video(:secure_url)?["\']',
            html,
            re.IGNORECASE,
        )
    if match:
        for group in match.groups():
            if group and isinstance(group, str) and ".mp4" in group:
                return group if group.startswith("https") else None

    # Fallback: Many video pins lack og:video but embed the MP4 stream directly in the JSON script payload.
    # We prefer 720p/mc format if available.
    match = re.search(
        r'(https?://[a-zA-Z0-9_\-\.]+\.pinimg\.com/videos/mc/[^"\']+\.mp4)\b', html
    )
    if match:
        return match.group(1)

    # Any other MP4 fallback
    match = re.search(
        r'(https?://[a-zA-Z0-9_\-\.]+\.pinimg\.com/videos/[^"\']+\.mp4)\b', html
    )
    if match:
        return match.group(1)

    return None


def _extract_og_image(html: str) -> Optional[str]:
    """Extract og:image from HTML (for static image pins)."""
    match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html,
            re.IGNORECASE,
        )
    if match:
        url = match.group(1)
        if url and url.startswith("https"):
            return url
    return None


async def _stream_download(direct_url: str, prefix: str, ext: str) -> Optional[str]:
    """Download a file using chunked async streaming (low memory usage)."""
    out_p = os.path.join(TEMP_DIR, f"{prefix}{uuid.uuid4().hex}.{ext}")
    try:
        async with AsyncSession() as session:
            resp = await session.get(
                direct_url,
                impersonate="chrome",
                timeout=60,
                stream=True,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "[PINTEREST] Stream download got HTTP %d for %s",
                    resp.status_code,
                    direct_url[:80],
                )
                return None

            total_bytes = 0

            def _write_chunks(path: str, data: bytes) -> None:
                mode = "ab" if os.path.exists(path) else "wb"
                with open(path, mode) as f:
                    f.write(data)

            async for chunk in resp.aiter_content():
                if chunk:
                    await asyncio.to_thread(_write_chunks, out_p, chunk)
                    total_bytes += len(chunk)

        if total_bytes == 0:
            await asyncio.to_thread(safe_remove, out_p)
            return None

        logger.info(
            "[PINTEREST] Stream download OK: %.1f MB -> %s",
            total_bytes / (1024 * 1024),
            out_p,
        )
        return out_p

    except Exception as e:
        logger.warning("[PINTEREST] Stream download failed: %s", e)
        if os.path.exists(out_p):
            await asyncio.to_thread(safe_remove, out_p)
        return None


async def _cobalt_fallback(
    url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Cobalt fallback for Pinterest — handles both video and picker responses."""
    from app.services.cobalt import CobaltService

    logger.info("[PINTEREST] Using Cobalt Fallback for %s", url)
    c_res = await CobaltService.process(url)

    if c_res.status == "error" or (not c_res.url and not c_res.picker):
        return (
            None,
            c_res.error_message
            or "Не удалось извлечь медиа с Pinterest (Нативный метод и Cobalt не справились)",
        )

    # Cobalt returned a direct video URL
    if c_res.url:
        result = await _stream_download(c_res.url, "pin_cobalt_", "mp4")
        if result:
            return result, None
        return None, "Ошибка загрузки через резервный канал"

    # Cobalt returned picker (carousel) — download the first item as a video/image
    if c_res.picker:
        first_item = c_res.picker[0]
        ext = "mp4" if first_item.type == "video" else "jpg"
        result = await _stream_download(first_item.url, "pin_cobalt_pick_", ext)
        if result:
            return result, None
        return None, "Ошибка загрузки из карусели Cobalt"

    return None, "Cobalt вернул неожиданный ответ"
