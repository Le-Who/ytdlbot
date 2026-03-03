"""Slideshow download and cleanup service."""

import os
import asyncio
import logging
import shutil
from typing import Optional, Tuple, TYPE_CHECKING

from app.core import state

if TYPE_CHECKING:
    from app.services.gallery_dl.service import SlideshowResult

logger = logging.getLogger("app.services.slideshow")


class SlideshowPipeline:
    """Handles downloading and cleaning up slideshow images."""

    @staticmethod
    async def download_slideshow(
        page_url: str,
    ) -> Tuple[Optional["SlideshowResult"], Optional[str]]:
        """
        Downloads TikTok slideshow images (and optional audio) via gallery-dl.

        Returns:
            (SlideshowResult, None) on success.
            (None, error_message) on failure.
        """
        from app.services.gallery_dl.service import GalleryDlService

        cookies_path = state.ytdlp.cookies_path
        proxy = state.ytdlp.tiktok_proxy

        result, error = await asyncio.to_thread(
            GalleryDlService.download_slideshow, page_url, cookies_path, proxy
        )

        return result, error

    @staticmethod
    def cleanup_slideshow(result) -> None:
        """Removes all downloaded slideshow files and their directory."""
        if not result or not result.images:
            return
        parent_dir = os.path.dirname(result.images[0])
        if parent_dir and os.path.isdir(parent_dir) and "slideshow_" in parent_dir:
            try:
                shutil.rmtree(parent_dir, ignore_errors=True)
                logger.info(f"[CLEANUP] Removed slideshow dir: {parent_dir}")
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to remove slideshow dir: {e}")
