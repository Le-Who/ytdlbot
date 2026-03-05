"""Media conversion service — GIF and slideshow-to-video via ffmpeg."""

import os
import uuid
import asyncio
import logging
from typing import Optional

from app.core import state
from app.core.config import TEMP_DIR
from app.core.utils import safe_remove

logger = logging.getLogger("app.services.converter")


class MediaConverter:
    """Handles ffmpeg-based media conversions."""

    @staticmethod
    async def convert_to_gif_ffmpeg(video_path: str) -> Optional[str]:
        """
        Converts a video to a mute MP4 (Telegram treats as GIF).

        Uses stream copy (no re-encoding) — instant, IO-bound only.
        """
        if not video_path or not os.path.exists(video_path):
            return None

        gif_path = video_path.rsplit(".", 1)[0] + "_gif.mp4"

        cmd = [
            "ffmpeg",
            "-y",
            "-t",
            "60",
            "-i",
            video_path,
            "-c:v",
            "copy",
            "-an",
            gif_path,
        ]

        try:
            # Limit concurrency for CPU-intensive conversions
            from app.core.metrics import metrics as _m
            async with state.conversion_sem:
                with _m.conversion_duration.time(type="gif"):
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=300.0
                        )
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        logger.error("FFmpeg conversion timed out")
                        return None

            if proc.returncode != 0:
                logger.error(f"FFmpeg conversion failed: {stderr.decode()}")
                return None

            if not os.path.exists(gif_path) or os.path.getsize(gif_path) == 0:
                return None

            return gif_path
        except Exception as e:
            logger.error(f"FFmpeg exception: {e}")
            return None

    @staticmethod
    async def images_to_video(
        images: list[str],
        audio_path: Optional[str] = None,
    ) -> Optional[str]:
        """
        Converts a list of images (+ optional audio) into a slideshow MP4.

        Each image is displayed for ~3 seconds. If audio exists, the video
        duration matches the audio length (with -shortest).

        Returns:
            Path to output MP4, or None on failure.
        """
        if not images:
            return None

        output_path = os.path.join(TEMP_DIR, f"slideshow_{uuid.uuid4().hex}.mp4")
        concat_file = os.path.join(TEMP_DIR, f"concat_{uuid.uuid4().hex}.txt")

        try:
            with open(concat_file, "w", encoding="utf-8") as f:
                for img in images:
                    escaped = img.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
                    f.write("duration 3\n")
                if images:
                    escaped = images[-1].replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
            ]

            if audio_path and os.path.exists(audio_path):
                cmd.extend(["-i", audio_path])
                cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale='min(1080,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-shortest",
                    "-movflags", "+faststart",
                    output_path,
                ])
            else:
                cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale='min(1080,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-movflags", "+faststart",
                    output_path,
                ])

            from app.core.metrics import metrics as _m
            async with state.conversion_sem:
                with _m.conversion_duration.time(type="slideshow"):
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    try:
                        _, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=300.0
                        )
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        logger.error("FFmpeg slideshow conversion timed out")
                        return None

            if proc.returncode != 0:
                logger.error(
                    f"FFmpeg slideshow failed: {stderr.decode('utf-8', errors='ignore')}"
                )
                return None

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return None

            return output_path

        except Exception as e:
            logger.error(f"Slideshow conversion exception: {e}", exc_info=True)
            return None
        finally:
            safe_remove(concat_file)
