"""
TikWM API service — third-party fallback for downloading TikTok videos.

Used when yt-dlp and gallery-dl both fail on age-restricted/classified content.
TikWM returns direct CDN video URLs regardless of datacenter IP restrictions.

API: https://tikwm.com/api/?url=<tiktok_url>&hd=1
Free tier: 5000 requests/day, 1 request/second.
"""

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import quote

from curl_cffi.requests import AsyncSession, Response

from app.core.config import TEMP_DIR

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
        api_url = f"{API_BASE}?url={quote(url, safe='')}&hd=1"
        last_error: Optional[str] = None

        for attempt in range(MAX_RETRIES):
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
            if code != 0:
                msg = data.get("msg", "unknown error")
                logger.warning("[TIKWM] API returned code=%s: %s", code, msg)
                return TikWMResult(status="error", error_message=f"TikWM: {msg}")

            info = data.get("data", {})
            title = info.get("title", "TikTok Media")

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
                return TikWMResult(status="video", title=title, url=video_url)

            logger.warning("[TIKWM] Unrecognized response format (no video or images)")
            return TikWMResult(
                status="error", error_message="TikWM: No video or images in response"
            )

        return TikWMResult(
            status="error", error_message=last_error or "TikWM: max retries exceeded"
        )

    @staticmethod
    async def download_video(url: str) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch video URL via TikWM API, then download the video to a temp file.

        Returns:
            (file_path, None) on success.
            (None, error_message) on failure.
        """
        res = await TikWMService.process(url)
        if res.status != "video" or not res.url:
            return None, res.error_message or "TikWM: Not a video"

        video_url = res.url

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

                with open(output_path, "wb") as f:
                    f.write(resp.content)

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info("[TIKWM] Downloaded: %s (%.1f MB)", output_path, size_mb)
            return output_path, None

        except Exception as e:
            logger.error("[TIKWM] Download failed: %s", e)
            # Clean up partial file
            if os.path.exists(output_path):
                os.unlink(output_path)
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
                with open(output_path, "wb") as f:
                    f.write(resp.content)

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info("[TIKWM] Downloaded audio: %s (%.1f MB)", output_path, size_mb)
            return output_path
        except Exception as e:
            logger.error("[TIKWM] Audio download failed: %s", e)
            if os.path.exists(output_path):
                os.unlink(output_path)
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
                for idx, img_url in enumerate(res.images):
                    out_path = os.path.join(
                        TEMP_DIR, f"tikwm_slide_{uuid.uuid4().hex}_{idx}.jpg"
                    )
                    resp = await session.get(img_url, impersonate="chrome", timeout=30)
                    with open(out_path, "wb") as f:
                        f.write(resp.content)
                    image_paths.append(out_path)
        except Exception as e:
            logger.error("TikWM slideshow image download failed: %s", e)
            # Cleanup what we have
            for p in image_paths:
                if os.path.exists(p):
                    os.unlink(p)
            return [], None

        audio_path = None
        if res.audio_url:
            audio_path = await TikWMService.download_audio(res.audio_url)

        return image_paths, audio_path
