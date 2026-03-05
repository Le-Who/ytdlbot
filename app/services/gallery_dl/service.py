"""
gallery-dl wrapper service for downloading TikTok content.

gallery-dl is used via subprocess since it has no stable Python API.
Supports both slideshow (image) and video downloads.
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
    """Wrapper around gallery-dl CLI for TikTok content downloads."""

    TIMEOUT = 120  # seconds

    @staticmethod
    def download_slideshow(
        url: str,
        cookies_path: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> tuple[Optional[SlideshowResult], Optional[str]]:
        """
        Downloads a TikTok slideshow (images + optional audio).

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
        ]

        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if proxy:
            cmd.extend(["--proxy", proxy])

        cmd.extend(["--", url])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GalleryDlService.TIMEOUT,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error("gallery-dl failed", extra={"stderr": stderr})

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
            logger.error("gallery-dl unexpected error", extra={"error": str(e)}, exc_info=True)
            return None, "⚠️ Внутренняя ошибка при загрузке."

        # Collect downloaded files
        return GalleryDlService._collect_files(output_dir)

    @staticmethod
    def download_video(
        url: str,
        cookies_path: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Downloads a TikTok video via gallery-dl.
        Used as fallback when yt-dlp can't access classified/restricted content.

        Returns:
            (video_path, None) on success.
            (None, error_message) on failure.
        """
        output_dir = os.path.join(TEMP_DIR, f"gdl_video_{uuid.uuid4().hex}")
        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            "gallery-dl",
            "--directory", output_dir,
            "--filename", "{id}.{extension}",
            "--no-mtime",
        ]

        if cookies_path:
            cmd.extend(["--cookies", cookies_path])
        if proxy:
            cmd.extend(["--proxy", proxy])

        cmd.extend(["--", url])

        logger.info("Attempting TikTok video download", extra={"url": url})

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=GalleryDlService.TIMEOUT,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error("Video download failed", extra={"stderr": stderr})
                return None, f"gallery-dl error: {stderr[:200]}"

        except FileNotFoundError:
            logger.error("[GALLERY-DL] gallery-dl not found in PATH")
            return None, "gallery-dl not installed"
        except subprocess.TimeoutExpired:
            logger.error("[GALLERY-DL] Video download timed out")
            return None, "gallery-dl timeout"
        except Exception as e:
            logger.error("Video download unexpected error", extra={"error": str(e)}, exc_info=True)
            return None, str(e)

        # Find the video file
        video_extensions = {".mp4", ".webm", ".mkv", ".mov"}
        for root, _dirs, files in os.walk(output_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in video_extensions:
                    video_path = os.path.join(root, fname)
                    size_mb = os.path.getsize(video_path) / (1024 * 1024)
                    logger.info(
                        f"[GALLERY-DL] Video downloaded: {video_path} "
                        f"({size_mb:.1f} MB)"
                    )
                    return video_path, None

        # No video found — might be a slideshow or failed extraction
        logger.warning("No video file found", extra={"dir": output_dir})
        return None, "No video file found"

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
            logger.warning("No images found", extra={"dir": output_dir})
            return None, "⚠️ Не удалось найти фото в слайдшоу."

        logger.info(
            f"[GALLERY-DL] Found {len(images)} images, "
            f"audio={'yes' if audio else 'no'}"
        )

        return SlideshowResult(images=images, audio=audio, title=title), None
