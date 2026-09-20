"""
Cobalt API service — primary backend for downloading TikTok videos and slideshows.

API Documentation: https://github.com/imputnet/cobalt
"""

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession, Response

from app.core.config import COBALT_API_KEY, COBALT_API_URLS, TEMP_DIR

__all__ = ["CobaltPickerItem", "CobaltResult", "CobaltService"]

logger = logging.getLogger("app.services.cobalt")

TIMEOUT = 15  # seconds
MAX_RETRIES = 2
CHUNK_SIZE = 1024 * 1024
MAX_MEDIA_BYTES = 2_000_000_000


async def _stream_response(response: Response, output_path: str) -> int:
    """Stream one response with the shared decimal media cap."""
    written = 0
    output = await asyncio.to_thread(open, output_path, "wb")
    try:
        async for chunk in response.aiter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            written += len(chunk)
            if written > MAX_MEDIA_BYTES:
                raise ValueError("media exceeds 2,000,000,000 byte limit")
            await asyncio.to_thread(output.write, chunk)
    finally:
        await asyncio.to_thread(output.close)
    declared = response.headers.get("content-length")
    if isinstance(declared, (str, int)):
        try:
            expected = int(declared)
        except ValueError:
            expected = -1
        if expected >= 0 and written != expected:
            raise ValueError("truncated media response")
    return written


@dataclass
class CobaltPickerItem:
    """An individual item in a picker response (e.g., a photo in a slideshow)."""

    type: str  # 'photo', 'video', 'gif'
    url: str
    thumb: Optional[str] = None


@dataclass
class CobaltResult:
    """Result of a Cobalt API request."""

    status: str  # 'tunnel', 'redirect', 'picker', 'error'
    url: Optional[str] = None  # Direct video URL
    filename: Optional[str] = None
    # For picker (slideshows)
    audio: Optional[str] = None
    audio_filename: Optional[str] = None
    picker: List[CobaltPickerItem] = field(default_factory=list)
    # Custom error field
    error_message: Optional[str] = None

    @property
    def is_slideshow(self) -> bool:
        return self.status == "picker" and any(p.type == "photo" for p in self.picker)


class CobaltService:
    """Interacts with the Cobalt API to resolve media URLs."""

    @staticmethod
    def _get_headers(api_base: str) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ytdlbot/2.0 (FastAPI)",
        }
        # Do not send the API key to the public instance
        if COBALT_API_KEY and "api.cobalt.tools" not in api_base.lower():
            headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
        return headers

    @staticmethod
    async def process(url: str, video_quality: str = "1080") -> CobaltResult:
        """
        Processes a URL via the Cobalt API using configured instances sequentially.

        Returns a CobaltResult object. If Cobalt fails across all instances, returns status='error'.
        """
        if not COBALT_API_URLS:
            return CobaltResult(
                status="error", error_message="No Cobalt API URLs configured"
            )

        payload = {
            "url": url,
            "videoQuality": video_quality,
            "filenameStyle": "basic",
        }

        last_error: Optional[str] = None

        for api_base in COBALT_API_URLS:
            api_url = f"{api_base.rstrip('/')}/"

            for attempt in range(MAX_RETRIES):
                try:
                    async with AsyncSession() as session:
                        resp: Response = await session.post(
                            api_url,
                            json=payload,
                            headers=CobaltService._get_headers(api_base),
                            timeout=TIMEOUT,
                        )

                        if resp.status_code >= 500:
                            raise Exception(f"Server error {resp.status_code}")

                        data: Dict[str, Any] = resp.json()

                        if "status" not in data:
                            raise Exception("Invalid response: missing 'status' field")

                        status = data["status"]

                        if status == "error":
                            err_code = data.get("error", {}).get(
                                "code", "unknown_error"
                            )
                            # A business error belongs to this exact origin. Another
                            # configured origin may still resolve the same public URL.
                            last_error = f"Cobalt error: {err_code}"
                            break

                        if status in ("tunnel", "redirect"):
                            return CobaltResult(
                                status=status,
                                url=data.get("url"),
                                filename=data.get("filename"),
                            )

                        if status == "picker":
                            items = []
                            for item in data.get("picker", []):
                                items.append(
                                    CobaltPickerItem(
                                        type=item.get("type", "unknown"),
                                        url=item.get("url", ""),
                                        thumb=item.get("thumb"),
                                    )
                                )
                            return CobaltResult(
                                status="picker",
                                audio=data.get("audio"),
                                audio_filename=data.get("audioFilename"),
                                picker=items,
                            )

                        # local-processing is not supported yet (requires ffmpeg glue logic for Cobalt)
                        return CobaltResult(
                            status="error",
                            error_message=f"Unsupported Cobalt status: {status}",
                        )

                except Exception as e:
                    last_error = str(e)
                    logger.warning(
                        "[COBALT] Attempt %d/%d for %s failed: %s",
                        attempt + 1,
                        MAX_RETRIES,
                        api_base,
                        str(e),
                    )
                    continue

            logger.warning(
                "[COBALT] All attempts failed for %s, moving to next fallback instance",
                api_base,
            )

        return CobaltResult(
            status="error",
            error_message=last_error or "Max retries exceeded across all instances",
        )

    @staticmethod
    async def download_file(url: str, ext: str) -> Optional[str]:
        """Generic binary file downloader (for video, images, audio)."""
        output_path = os.path.join(TEMP_DIR, f"cblt_{uuid.uuid4().hex}.{ext}")
        try:
            async with AsyncSession() as session:
                resp = await session.get(
                    url, impersonate="chrome", timeout=60, stream=True
                )
                if resp.status_code >= 400:
                    logger.error(
                        "[COBALT] File download failed: HTTP %s", resp.status_code
                    )
                    return None

                await _stream_response(resp, output_path)
            return output_path
        except Exception as e:
            logger.error("[COBALT] File download error: %s", e)
            if os.path.exists(output_path):
                os.unlink(output_path)
            return None

    @staticmethod
    async def download_slideshow(
        result: CobaltResult,
    ) -> tuple[Optional[List[str]], Optional[str]]:
        """
        Downloads a slideshow's images and audio to a temp directory.
        Returns: (list_of_image_paths, audio_path) or (None, None) on error.
        """
        if not result.is_slideshow:
            return None, None

        slideshow_id = uuid.uuid4().hex
        base_dir = os.path.join(TEMP_DIR, f"slideshow_{slideshow_id}")
        os.makedirs(base_dir, exist_ok=True)

        image_urls = [p.url for p in result.picker if p.type == "photo" and p.url]
        tasks = []

        # Download images concurrently
        async def dl_image(idx: int, img_url: str) -> str:
            async with AsyncSession() as session:
                resp = await session.get(
                    img_url, impersonate="chrome", timeout=30, stream=True
                )
                path = os.path.join(base_dir, f"{idx:03d}.jpg")
                if resp.status_code != 200:
                    raise RuntimeError(f"media returned HTTP {resp.status_code}")
                await _stream_response(resp, path)
                return path

        for i, url in enumerate(image_urls):
            tasks.append(dl_image(i, url))

        try:
            image_paths = await asyncio.gather(*tasks)
        except Exception as e:
            logger.error("[COBALT] Slideshow image download error: %s", e)
            return None, None

        audio_path = None
        if result.audio:
            try:
                async with AsyncSession() as session:
                    resp = await session.get(
                        result.audio, impersonate="chrome", timeout=30, stream=True
                    )
                    audio_path = os.path.join(base_dir, "audio.mp3")
                    if resp.status_code != 200:
                        raise RuntimeError(f"media returned HTTP {resp.status_code}")
                    await _stream_response(resp, audio_path)
            except Exception as e:
                logger.error("[COBALT] Slideshow audio download error: %s", e)
                # It's okay to proceed without audio

        return list(image_paths), audio_path
