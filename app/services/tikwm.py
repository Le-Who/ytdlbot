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
from typing import Optional
from urllib.parse import quote

from curl_cffi.requests import AsyncSession, Response

from app.core.config import TEMP_DIR

__all__ = ["TikWMService"]

logger = logging.getLogger("app.services.tikwm")

API_BASE = "https://tikwm.com/api/"
TIMEOUT = 15  # seconds
MAX_RETRIES = 3
CHUNK_SIZE = 1024 * 1024  # 1 MB


class TikWMService:
    """Fetches TikTok video URLs via the TikWM public API."""

    @staticmethod
    async def fetch_video(
        url: str,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Fetch a TikTok video URL via TikWM API.

        Returns:
            (video_url, title, None) on success.
            (None, None, error_message) on failure.
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
                return None, None, f"TikWM: {msg}"

            info = data.get("data", {})
            video_url = info.get("hdplay") or info.get("play")
            title = info.get("title", "TikTok Video")

            if not video_url:
                logger.warning("[TIKWM] No video URL in response")
                return None, None, "TikWM: no video URL in response"

            logger.info(
                "[TIKWM] Got video URL (duration=%ss, title=%s)",
                info.get("duration", "?"),
                title[:60],
            )
            return video_url, title, None

        return None, None, last_error or "TikWM: max retries exceeded"

    @staticmethod
    async def download_video(url: str) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch video URL via TikWM API, then download the video to a temp file.

        Returns:
            (file_path, None) on success.
            (None, error_message) on failure.
        """
        video_url, title, error = await TikWMService.fetch_video(url)
        if error:
            return None, error

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
