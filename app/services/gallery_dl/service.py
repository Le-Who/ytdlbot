"""
gallery-dl wrapper service for downloading TikTok slideshow images.

gallery-dl is used via subprocess since it has no stable Python API.
"""

import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.core.config import TEMP_DIR

__all__ = ["GalleryDlService", "SlideshowResult"]

logger = logging.getLogger("app.services.gallery_dl")


@dataclass
class SlideshowResult:
    """Result of a slideshow download."""

    images: list[str] = field(default_factory=list)  # Absolute paths to images
    audio: Optional[str] = None  # Path to audio file (if any)
    title: str = ""


class GalleryDlService:
    """Wrapper around gallery-dl CLI for TikTok slideshow downloads."""

    TIMEOUT = 120  # seconds

    @staticmethod
    def download_slideshow(
        url: str,
        cookies_path: Optional[str] = None,
    ) -> tuple[Optional[SlideshowResult], Optional[str]]:
        """
        Downloads a TikTok slideshow (images + optional audio).

        Args:
            url: TikTok post URL.
            cookies_path: Path to Netscape cookies file.

        Returns:
            (SlideshowResult, None) on success.
            (None, error_message) on failure.
        """
        output_dir = os.path.join(TEMP_DIR, f"slideshow_{uuid.uuid4().hex}")
        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            "gallery-dl",
            "--directory", output_dir,
            "--filename", "{num:>03}.{extension}",
            "--no-mtime",
            "--write-metadata",
            url,
        ]

        if cookies_path:
            cmd.extend(["--cookies", cookies_path])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GalleryDlService.TIMEOUT,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error(f"[GALLERY-DL] Failed: {stderr}")

                if "login" in stderr.lower() or "cookie" in stderr.lower():
                    return None, "⚠️ Требуется авторизация (Sign-in required)."
                return None, "⚠️ Ошибка загрузки слайдшоу."

        except FileNotFoundError:
            logger.error("[GALLERY-DL] gallery-dl not found in PATH")
            return None, "⚠️ gallery-dl не установлен."
        except subprocess.TimeoutExpired:
            logger.error("[GALLERY-DL] Download timed out")
            return None, "⚠️ Время ожидания загрузки истекло."
        except Exception as e:
            logger.error(f"[GALLERY-DL] Unexpected error: {e}", exc_info=True)
            return None, "⚠️ Внутренняя ошибка при загрузке."

        # Collect downloaded files
        return GalleryDlService._collect_files(output_dir)

    @staticmethod
    def _collect_files(
        output_dir: str,
    ) -> tuple[Optional[SlideshowResult], Optional[str]]:
        """Scans the output directory for downloaded images and audio."""
        images: list[str] = []
        audio: Optional[str] = None
        title = ""

        image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        audio_extensions = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}

        # gallery-dl may create subdirectories, so we walk recursively
        for root, _dirs, files in os.walk(output_dir):
            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                ext = os.path.splitext(fname)[1].lower()

                if ext in image_extensions:
                    images.append(fpath)
                elif ext in audio_extensions and audio is None:
                    audio = fpath
                elif ext == ".json":
                    # Try to extract title from metadata
                    try:
                        with open(fpath, encoding="utf-8") as f:
                            meta = json.load(f)
                        if not title:
                            title = (
                                meta.get("description", "")
                                or meta.get("title", "")
                                or ""
                            )
                            # Truncate long descriptions
                            if len(title) > 200:
                                title = title[:197] + "..."
                    except Exception:
                        pass

        if not images:
            logger.warning(f"[GALLERY-DL] No images found in {output_dir}")
            return None, "⚠️ Не удалось найти фото в слайдшоу."

        logger.info(
            f"[GALLERY-DL] Found {len(images)} images, "
            f"audio={'yes' if audio else 'no'}"
        )

        return SlideshowResult(images=images, audio=audio, title=title), None
