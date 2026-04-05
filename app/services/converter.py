"""Media conversion service — GIF and slideshow-to-video via ffmpeg."""

import json
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

# Target file size ceiling for Telegram Bot API uploads (bytes).
# Hard limit is 50 MB; we use 48.5 MB to absorb MP4 container/moov-atom overhead.
_TG_MAX_BYTES = int(48.5 * 1024 * 1024)

# Audio bitrate assumed for compression calculation when stream bitrate is unknown.
_FALLBACK_AUDIO_KBPS = 128

# Minimum acceptable video bitrate — below this quality degrades severely.
_MIN_VIDEO_KBPS = 100


async def _probe_full_meta(video_path: str) -> dict:
    """Probe duration, audio bitrate, and overall bitrate from a file via ffprobe.

    Returns a dict with keys:
        duration_s  (float | None)  — total duration in seconds
        audio_kbps  (int | None)    — audio stream bitrate in kbps
    """
    result: dict = {"duration_s": None, "audio_kbps": None}
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        video_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT)
        if not stdout:
            return result
        probe = json.loads(stdout)
        fmt = probe.get("format", {})
        dur = fmt.get("duration")
        if dur:
            result["duration_s"] = float(dur)
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "audio":
                br = stream.get("bit_rate")
                if br:
                    result["audio_kbps"] = int(br) // 1000
                break
    except Exception as exc:
        logger.debug("_probe_full_meta failed for %s: %s", video_path, exc)
    return result


async def compress_video_to_size(
    input_path: str,
    *,
    target_bytes: int = _TG_MAX_BYTES,
    audio_kbps: int = _FALLBACK_AUDIO_KBPS,
) -> Optional[str]:
    """Re-encode a video with a mathematically calculated bitrate to fit target_bytes.

    Algorithm (two-pass libx264):
      1. Probe the video's exact duration and audio bitrate.
      2. Compute required video bitrate:
             total_kbps  = (target_bytes * 8) / (duration_s * 1000)
             video_kbps  = total_kbps - audio_kbps
      3. Run FFmpeg pass 1 (analysis only, no output — fast).
      4. Run FFmpeg pass 2 (real encode at computed bitrate).

    This mirrors the "pixel-perfect" JPEG technique — encode precisely once with
    full information rather than guessing and retrying.

    Returns:
        Path to the compressed file, or None if compression failed/unnecessary.
    """
    if not input_path or not os.path.exists(input_path):
        return None

    # --- Guard: only compress if actually over limit ---
    current_size = os.path.getsize(input_path)
    if current_size <= target_bytes:
        logger.debug(
            "compress_video_to_size: %s is %d bytes, under limit — skip",
            input_path,
            current_size,
        )
        return None

    size_mb = current_size / (1024 * 1024)
    target_mb = target_bytes / (1024 * 1024)
    logger.info(
        "Video is %.1f MB > %.1f MB limit — running two-pass size-targeting compression",
        size_mb,
        target_mb,
    )

    # --- Step 1: probe duration & audio bitrate ---
    meta = await _probe_full_meta(input_path)
    duration_s = meta["duration_s"]
    if not duration_s or duration_s <= 0:
        logger.error("compress_video_to_size: cannot probe duration for %s", input_path)
        return None

    probed_audio_kbps = meta.get("audio_kbps") or audio_kbps

    # --- Step 2: compute exact video bitrate ---
    total_kbps = (target_bytes * 8) / (duration_s * 1000)
    video_kbps = int(total_kbps - probed_audio_kbps)

    if video_kbps < _MIN_VIDEO_KBPS:
        logger.warning(
            "compress_video_to_size: computed video_kbps=%d is below minimum %d — "
            "video is too long to compress within %.1f MB at acceptable quality",
            video_kbps,
            _MIN_VIDEO_KBPS,
            target_mb,
        )
        return None

    logger.info(
        "compress_video_to_size: duration=%.1fs, audio=%dkbps, "
        "total=%.0fkbps → video_kbps=%d",
        duration_s,
        probed_audio_kbps,
        total_kbps,
        video_kbps,
    )

    # --- Step 3 & 4: two-pass encode ---
    uid = uuid.uuid4().hex
    output_path = input_path.rsplit(".", 1)[0] + f"_compressed_{uid}.mp4"
    # FFmpeg writes pass-log files alongside the passlogfile prefix.
    passlog_prefix = os.path.join(TEMP_DIR, f"ffmpeg2pass_{uid}")

    common_video_args = [
        "-c:v",
        "libx264",
        "-b:v",
        f"{video_kbps}k",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(_FFMPEG_THREADS),
        "-passlogfile",
        passlog_prefix,
    ]

    # Pass 1: analysis only (no output written, -f null /dev/null)
    pass1_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        *common_video_args,
        "-pass",
        "1",
        "-an",
        "-f",
        "null",
        os.devnull,  # Cross-platform safe (NUL on Windows, /dev/null on POSIX)
    ]

    # Pass 2: actual encode
    pass2_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        *common_video_args,
        "-pass",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        f"{probed_audio_kbps}k",
        "-movflags",
        "+faststart",
        output_path,
    ]

    async def _run(cmd: list[str], label: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_FFMPEG_TIMEOUT
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "timeout"
        return proc.returncode or 0, stderr.decode("utf-8", errors="ignore")

    try:
        async with state.conversion_sem:
            rc1, err1 = await _run(pass1_cmd, "pass1")
            if rc1 != 0:
                logger.error(
                    "compress_video_to_size pass1 failed (rc=%d): %s",
                    rc1,
                    err1[-500:],
                )
                return None

            rc2, err2 = await _run(pass2_cmd, "pass2")
            if rc2 != 0:
                logger.error(
                    "compress_video_to_size pass2 failed (rc=%d): %s",
                    rc2,
                    err2[-500:],
                )
                safe_remove(output_path)
                return None

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error("compress_video_to_size: output file missing or empty")
            safe_remove(output_path)
            return None

        final_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(
            "compress_video_to_size: %.1f MB → %.1f MB (target %.1f MB) ✓",
            size_mb,
            final_size_mb,
            target_mb,
        )
        return output_path

    except Exception as exc:
        logger.error("compress_video_to_size exception: %s", exc, exc_info=True)
        safe_remove(output_path)
        return None
    finally:
        # Clean up the two-pass log files (ffmpeg writes <prefix>-0.log etc.)
        for suffix in ("-0.log", "-0.log.mbtree"):
            safe_remove(passlog_prefix + suffix)


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

        return proc.returncode or 0, stderr.decode("utf-8", errors="ignore")

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
    async def convert_to_native_gif(video_path: str) -> Optional[str]:
        """Convert a video file to a native .gif with high-quality palette generation.

        Uses FFmpeg's two-pass palettegen→paletteuse pipeline for optimal color
        quantization (256 colors), capped at 480px width and 15fps for a good
        balance of visual quality and file size.

        This is an on-demand operation — only called when the user explicitly
        requests the native file format. Uses gif_file_sem for bounded concurrency.

        Returns:
            Path to the output .gif file, or None on failure.
        """
        if not video_path or not os.path.exists(video_path):
            logger.warning("convert_to_native_gif: source not found: %s", video_path)
            return None

        base = video_path.rsplit(".", 1)[0]
        palette_path = os.path.join(TEMP_DIR, f"palette_{uuid.uuid4().hex}.png")
        gif_path = f"{base}_native.gif"

        # Scale to max 480px wide, 15fps; odd-dimension safety with force_divisible_by=2
        scale_filter = (
            "fps=15,scale=w='min(480,iw)':h=-2:force_divisible_by=2:flags=lanczos"
        )

        pass1_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vf",
            f"{scale_filter},palettegen=max_colors=256:stats_mode=diff",
            "-frames:v",
            "1",
            palette_path,
        ]

        pass2_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-i",
            palette_path,
            "-filter_complex",
            f"[0:v]{scale_filter}[scaled];[scaled][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
            "-loop",
            "0",
            "-an",
            gif_path,
        ]

        async def _run(cmd: list[str]) -> tuple[int, str]:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_FFMPEG_TIMEOUT
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return -1, "timeout"
            return proc.returncode or 0, stderr.decode("utf-8", errors="ignore")

        try:
            from app.core import state as _state

            async with _state.gif_file_sem:
                # Pass 1: generate palette image
                rc1, err1 = await _run(pass1_cmd)
                if rc1 != 0:
                    logger.error(
                        "convert_to_native_gif: palettegen failed (rc=%d): %s",
                        rc1,
                        err1[-400:],
                    )
                    return None

                # Pass 2: render gif using palette
                rc2, err2 = await _run(pass2_cmd)
                if rc2 != 0:
                    logger.error(
                        "convert_to_native_gif: paletteuse failed (rc=%d): %s",
                        rc2,
                        err2[-400:],
                    )
                    safe_remove(gif_path)
                    return None

            if not os.path.exists(gif_path) or os.path.getsize(gif_path) == 0:
                logger.error("convert_to_native_gif: output empty or missing")
                return None

            size_mb = os.path.getsize(gif_path) / (1024 * 1024)
            logger.info("convert_to_native_gif: OK → %s (%.1f MB)", gif_path, size_mb)
            return gif_path

        except Exception as exc:
            logger.error("convert_to_native_gif exception: %s", exc, exc_info=True)
            safe_remove(gif_path)
            return None
        finally:
            safe_remove(palette_path)

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
