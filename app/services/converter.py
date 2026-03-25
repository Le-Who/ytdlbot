"""Media conversion service — GIF and slideshow-to-video via ffmpeg."""

import os
import uuid
import asyncio
import logging
import struct
from typing import Optional

from app.core import state
from app.core.config import TEMP_DIR
from app.core.utils import safe_remove

logger = logging.getLogger("app.services.converter")

# ffmpeg encoding thread budget per conversion job.
_FFMPEG_THREADS = 2

# Codecs that can be stream-copied into an MP4 container without re-encoding.
_MP4_SAFE_CODECS = frozenset({"h264", "hevc", "h265", "mpeg4", "avc"})

# Timeout for ffprobe / ffmpeg subprocesses (seconds).
_FFPROBE_TIMEOUT = 10.0
_FFMPEG_TIMEOUT = 300.0


async def _probe_video_codec(video_path: str) -> Optional[str]:
    """Detect the video codec of a file via ffprobe.

    Returns the lowercase codec name (e.g. ``"h264"``, ``"vp9"``) or
    ``None`` if probing fails.  Runs in ~100–200 ms.
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-print_format",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT)
        if proc.returncode == 0 and stdout:
            return stdout.decode().strip().lower()
    except Exception as exc:
        logger.debug("ffprobe failed: %s", exc)
    return None


def _get_audio_duration(audio_path: str) -> Optional[float]:
    """Fast extraction of MP3/M4A duration without ffprobe.

    Reads the file header to estimate duration.  Falls back to None
    if format is unrecognised (caller will use fixed 3s/image).
    """
    try:
        ext = os.path.splitext(audio_path)[1].lower()
        size = os.path.getsize(audio_path)
        if size == 0:
            return None

        if ext == ".mp3":
            # Rough estimate: assume ~128kbps CBR
            return size / (128_000 / 8)

        if ext in (".m4a", ".mp4", ".aac"):
            # Parse the mvhd atom for timescale/duration
            with open(audio_path, "rb") as f:
                data = f.read(min(size, 64 * 1024))  # first 64KB
            idx = data.find(b"mvhd")
            if idx >= 0:
                offset = idx + 4
                version = data[offset]
                if version == 0:
                    ts = struct.unpack(">I", data[offset + 12 : offset + 16])[0]
                    dur = struct.unpack(">I", data[offset + 16 : offset + 20])[0]
                else:
                    ts = struct.unpack(">I", data[offset + 20 : offset + 24])[0]
                    dur = struct.unpack(">Q", data[offset + 24 : offset + 32])[0]
                if ts > 0:
                    return float(dur / ts)
    except Exception as e:
        logger.debug("Audio duration probe failed: %s", e)
    return None


def _can_copy_audio(audio_path: str) -> bool:
    """Check if the audio file is already in a Telegram-compatible codec (MP3/AAC)."""
    ext = os.path.splitext(audio_path)[1].lower()
    return ext in (".mp3", ".m4a", ".aac")


class MediaConverter:
    """Handles ffmpeg-based media conversions."""

    @staticmethod
    async def _run_gif_ffmpeg(
        video_path: str,
        gif_path: str,
        *,
        use_copy: bool,
    ) -> tuple[int, str]:
        """Run a single ffmpeg GIF-strip pass.

        Returns ``(returncode, stderr_text)``.
        """
        if use_copy:
            codec_args = ["-c:v", "copy"]
        else:
            codec_args = [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                str(_FFMPEG_THREADS),
            ]

        cmd = [
            "ffmpeg",
            "-y",
            "-t",
            "60",
            "-i",
            video_path,
            *codec_args,
            "-an",
            gif_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_FFMPEG_TIMEOUT,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "timeout"

        return proc.returncode, stderr.decode("utf-8", errors="ignore")

    @staticmethod
    async def convert_to_gif_ffmpeg(video_path: str) -> Optional[str]:
        """Convert a video to a mute MP4 (Telegram treats as GIF).

        Strategy:
        1. Probe the video codec via ffprobe.
        2. If codec is MP4-safe (h264/hevc) → stream-copy (instant).
        3. Otherwise → transcode via libx264 (fast, universal).
        4. If stream-copy fails → auto-fallback to transcode.
        """
        if not video_path or not os.path.exists(video_path):
            return None

        gif_path = video_path.rsplit(".", 1)[0] + "_gif.mp4"

        try:
            from app.core.metrics import metrics as _m

            # Probe codec to decide copy vs transcode
            codec = await _probe_video_codec(video_path)
            use_copy = codec in _MP4_SAFE_CODECS if codec else False
            if codec:
                logger.info(
                    "GIF conversion: detected codec=%s, copy=%s",
                    codec,
                    use_copy,
                )

            async with state.conversion_sem:
                with _m.conversion_duration.time(type="gif"):
                    rc, stderr_text = await MediaConverter._run_gif_ffmpeg(
                        video_path,
                        gif_path,
                        use_copy=use_copy,
                    )

                    # Fallback: stream-copy failed → retry with transcode
                    if rc != 0 and use_copy:
                        logger.warning(
                            "Stream-copy failed (rc=%d), falling back to transcode",
                            rc,
                            extra={"stderr": stderr_text[:2000], "returncode": rc},
                        )
                        safe_remove(gif_path)
                        rc, stderr_text = await MediaConverter._run_gif_ffmpeg(
                            video_path,
                            gif_path,
                            use_copy=False,
                        )

            if rc != 0:
                _m.conversion_failures.inc(type="gif")
                logger.error(
                    "FFmpeg conversion failed (rc=%d): %s",
                    rc,
                    stderr_text[:500],
                    extra={
                        "stderr": stderr_text[:2000],
                        "returncode": rc,
                        "path": video_path,
                    },
                )
                return None

            if not os.path.exists(gif_path) or os.path.getsize(gif_path) == 0:
                return None

            return gif_path
        except Exception as e:
            logger.error("FFmpeg exception", extra={"error": str(e)})
            return None

    @staticmethod
    async def images_to_video(
        images: list[str],
        audio_path: Optional[str] = None,
    ) -> Optional[str]:
        """
        Converts a list of images (+ optional audio) into a slideshow MP4.

        Optimisations applied:
        - ultrafast preset + stillimage tune + CRF 28 (~40% faster encoding)
        - Adaptive duration: image_dur = audio_len / N (UX: video = full track)
        - Audio stream copy when source is MP3/M4A (skip re-encoding)
        - Explicit thread budget (-threads 2)

        Returns:
            Path to output MP4, or None on failure.
        """
        if not images:
            return None

        output_path = os.path.join(TEMP_DIR, f"slideshow_{uuid.uuid4().hex}.mp4")
        concat_file = os.path.join(TEMP_DIR, f"concat_{uuid.uuid4().hex}.txt")

        try:
            # --- Adaptive duration ---
            image_dur = 3.0  # default: 3 seconds per image
            if audio_path and os.path.exists(audio_path):
                audio_dur = await asyncio.to_thread(_get_audio_duration, audio_path)
                if audio_dur and audio_dur > 0 and len(images) > 0:
                    image_dur = audio_dur / len(images)
                    # Clamp between 1s and 10s to avoid extremes
                    image_dur = max(1.0, min(10.0, image_dur))
                    logger.info(
                        "Adaptive slideshow: %.1fs audio / %d images = %.2fs each",
                        audio_dur,
                        len(images),
                        image_dur,
                    )

            # --- Write concat file ---
            def _write_concat() -> None:
                with open(concat_file, "w", encoding="utf-8") as f:
                    for img in images:
                        escaped = img.replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")
                        f.write(f"duration {image_dur:.2f}\n")
                    if images:
                        escaped = images[-1].replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

            await asyncio.to_thread(_write_concat)

            # --- Build ffmpeg command ---
            # Shared video encoding parameters (optimised for static images)
            scale_filter = "scale=w='min(1080,iw)':h=-2:force_divisible_by=2"
            video_args = [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "stillimage",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                str(_FFMPEG_THREADS),
                "-vf",
                scale_filter,
            ]

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
            ]

            if audio_path and os.path.exists(audio_path):
                cmd.extend(["-i", audio_path])

                # Audio: stream copy if already MP3/AAC, else re-encode
                if _can_copy_audio(audio_path):
                    audio_args = ["-c:a", "copy"]
                else:
                    audio_args = ["-c:a", "aac", "-b:a", "128k"]

                cmd.extend(video_args)
                cmd.extend(audio_args)
                cmd.extend(["-shortest", "-movflags", "+faststart", output_path])
            else:
                cmd.extend(video_args)
                cmd.extend(["-movflags", "+faststart", output_path])

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
                    "FFmpeg slideshow failed: %s",
                    stderr.decode("utf-8", errors="ignore"),
                )
                return None

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return None

            return output_path

        except Exception as e:
            logger.error(
                "Slideshow conversion exception", extra={"error": str(e)}, exc_info=True
            )
            return None
        finally:
            safe_remove(concat_file)
