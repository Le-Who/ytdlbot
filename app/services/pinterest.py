"""
Pinterest Native Service
Instantly extracts direct MP4 media links from Pinterest URLs using open graph meta tags,
completely bypassing heavy yt-dlp overhead and 403 blocks.
"""

import os
import uuid
import logging
import asyncio
from typing import Optional, Tuple
from curl_cffi.requests import AsyncSession
import re

from app.core.config import TEMP_DIR
from app.core.utils import safe_remove
from app.services.cobalt import CobaltService

logger = logging.getLogger("app.services.pinterest")


class PinterestNativeService:
    @staticmethod
    async def extract_media_url(url: str) -> Optional[str]:
        """Scrape the og:video:secure_url using a fast curl_cffi GET request."""
        try:
            async with AsyncSession() as session:
                resp = await session.get(
                    url, impersonate="chrome", timeout=10, allow_redirects=True
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "[PINTEREST] Native scrape got HTTP %d", resp.status_code
                    )
                    return None

                html = resp.text
                # Look for <meta property="og:video:secure_url" content="..."> or og:video
                match = re.search(
                    r'<meta[^>]+property=["\']og:video(:secure_url)?["\'][^>]+content=["\']([^"\']+\.mp4(?:[?#][^"\']*)?)["\']',
                    html,
                    re.IGNORECASE,
                )
                if not match:
                    # Alternative structural check if content attribute comes first
                    match = re.search(
                        r'<meta[^>]+content=["\']([^"\']+\.mp4(?:[?#][^"\']*)?)["\'][^>]+property=["\']og:video(:secure_url)?["\']',
                        html,
                        re.IGNORECASE,
                    )

                if match:
                    # In python re.search, match.groups() will contain the captures.
                    # We just test which group contains the .mp4 string.
                    for group in match.groups():
                        if group and isinstance(group, str) and ".mp4" in group:
                            return group if group.startswith("https") else None

                logger.warning(
                    "[PINTEREST] Native scrape found no og:video MP4 url in HTML"
                )
                return None
        except Exception as e:
            logger.error("[PINTEREST] Native extraction error: %s", e)
            return None

    @staticmethod
    async def download_video(url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract direct MP4 and download it.
        Tier 1: Native Scraper
        Tier 2: Fallback to Cobalt (for cases like image carousels)
        """
        direct_url = await PinterestNativeService.extract_media_url(url)

        if direct_url:
            out_p = os.path.join(TEMP_DIR, f"pin_{uuid.uuid4().hex}.mp4")
            try:
                async with AsyncSession() as session:
                    resp = await session.get(
                        direct_url, impersonate="chrome", timeout=60
                    )
                    if resp.status_code >= 400:
                        raise Exception(f"HTTP {resp.status_code}")

                    def _write():
                        with open(out_p, "wb") as f:
                            f.write(resp.content)

                    await asyncio.to_thread(_write)

                file_size = await asyncio.to_thread(os.path.getsize, out_p)
                logger.info(
                    "[PINTEREST] Native Download successful: %.1f MB",
                    file_size / (1024 * 1024),
                )
                return out_p, None
            except Exception as e:
                logger.warning(
                    "[PINTEREST] Direct download failed, falling back to Cobalt. Err: %s",
                    e,
                )
                if os.path.exists(out_p):
                    await asyncio.to_thread(safe_remove, out_p)
                # Fallthrough to tier 2

        # Tier 2 Fallback to Cobalt
        logger.info("[PINTEREST] Using Cobalt Fallback for %s", url)
        c_res = await CobaltService.process(url)
        if c_res.status == "error" or not c_res.url:
            return (
                None,
                c_res.error_message
                or "Не удалось извлечь медиа с Pinterest (Нативный метод и Cobalt не справились)",
            )

        cobalt_out = os.path.join(TEMP_DIR, f"pin_cobalt_{uuid.uuid4().hex}.mp4")
        try:
            async with AsyncSession() as session:
                resp = await session.get(c_res.url, impersonate="chrome", timeout=60)
                if resp.status_code >= 400:
                    raise Exception(f"HTTP {resp.status_code}")

                def _write_cobalt():
                    with open(cobalt_out, "wb") as f:
                        f.write(resp.content)

                await asyncio.to_thread(_write_cobalt)
            return cobalt_out, None
        except Exception as e:
            if os.path.exists(cobalt_out):
                await asyncio.to_thread(safe_remove, cobalt_out)
            return None, f"Ошибка загрузки через резервный канал: {e}"
