import asyncio
import json
import os
import io
import logging
from typing import Optional, Callable, Awaitable, Union

from telegram.constants import ChatAction
from telegram import Bot

from app.core import state, config
from app.core.config import MAX_TG_UPLOAD_MB
from app.core.utils import safe_remove
from app.core.policy import size_allowed
from app.core.texts import Texts
from app.core.models import DownloadContext
from app.constants import GIF_FORMAT_ID, AUDIO_FORMAT_ID
from app.services.downloader import MediaSender
from app.services.tikwm import TikWMService
from app.services.gallery_dl.service import GalleryDlService
from app.services.pinterest import PinterestNativeService
from app.services.converter import compress_video_to_size, find_thumbnail, split_video_stream_copy

logger = logging.getLogger("app.services.orchestrator")

# Telegram-compatible video codecs — anything else needs re-encoding
_TG_SAFE_CODECS = {"h264", "mpeg4"}

# Telegram hard upload limit for URL-mode delivery (server-side fetch).
# Files above this must be downloaded locally and uploaded as binary.
_TG_URL_DELIVERY_MAX_BYTES = int(19.5 * 1024 * 1024)  # 19.5 MB

# Telegram 50 MB upload ceiling (Bot API).
_TG_UPLOAD_LIMIT_BYTES = int(48.5 * 1024 * 1024)  # 48.5 MB — absorbs moov-atom overhead


async def _extract_video_meta(
    file_path: str | object,
    info_json_path: Optional[str] = None,
) -> dict:
    """Extract duration/width/height/vcodec for Telegram send_video.

    Strategy: try info JSON first (zero-cost), fall back to ffprobe.
    Returns dict with keys: duration, width, height (all Optional[int]),
    vcodec (Optional[str]).
    """
    meta: dict = {"duration": None, "width": None, "height": None, "vcodec": None}

    if info_json_path and os.path.exists(info_json_path):
        try:
            with open(info_json_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            dur = info.get("duration")
            if dur:
                meta["duration"] = int(float(dur))
            if info.get("width"):
                meta["width"] = int(info["width"])
            if info.get("height"):
                meta["height"] = int(info["height"])
            meta["vcodec"] = info.get("vcodec")
            if meta["duration"]:
                return meta
        except Exception:
            pass

    if not isinstance(file_path, str):
        return meta

    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if stdout:
            probe = json.loads(stdout)
            fmt = probe.get("format", {})
            dur = fmt.get("duration")
            if dur:
                meta["duration"] = int(float(dur))
            stream: dict = next(
                (s for s in probe.get("streams", []) if s.get("codec_type") == "video"),
                {},
            )
            if stream.get("width"):
                meta["width"] = int(stream["width"])
            if stream.get("height"):
                meta["height"] = int(stream["height"])
            if stream.get("codec_name"):
                meta["vcodec"] = stream["codec_name"].lower()
            if stream.get("pix_fmt"):
                meta["pix_fmt"] = stream["pix_fmt"].lower()
            if stream.get("codec_tag_string"):
                meta["codec_tag"] = stream["codec_tag_string"].lower()
    except Exception as e:
        logger.warning("ffprobe meta extraction failed for %s: %s", file_path, e)

    return meta


async def _ensure_telegram_compatible(file_path: str) -> str:
    """Re-encode video to H.264/AAC if its codec is not Telegram-compatible.

    TikTok CDN often serves HEVC (H.265) videos which Telegram clients
    cannot play inline.  This function detects incompatible codecs via
    ffprobe and re-encodes with libx264 ultrafast.  H.264 videos pass
    through untouched (zero-cost happy path).

    Returns:
        Original path if already compatible, or path to re-encoded file.
    """
    meta = await _extract_video_meta(file_path)
    vcodec = meta.get("vcodec")
    pix_fmt = meta.get("pix_fmt")

    # If ffprobe failed completely (vcodec=None), it might be a broken container.
    # We should attempt re-encoding rather than passing it raw to Telegram.
    is_safe_codec = (vcodec is not None) and (vcodec in _TG_SAFE_CODECS)
    # Telegram cannot natively play 10-bit H.264 (yuv420p10le).
    is_safe_pix_fmt = not pix_fmt or "10" not in pix_fmt

    if is_safe_codec and is_safe_pix_fmt:
        return file_path  # already compatible — no re-encode

    logger.warning(
        "Video codec '%s' (pix_fmt: '%s') is not Telegram-compatible, re-encoding to H.264",
        vcodec,
        pix_fmt,
    )

    re_encoded = file_path.rsplit(".", 1)[0] + "_h264.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        file_path,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        re_encoded,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)

        if proc.returncode != 0:
            logger.error(
                "H.264 re-encode failed: %s",
                stderr.decode("utf-8", errors="ignore")[-500:],
            )
            safe_remove(re_encoded)
            return file_path  # fall back to original

        if not os.path.exists(re_encoded) or os.path.getsize(re_encoded) == 0:
            safe_remove(re_encoded)
            return file_path

        logger.info(
            "Re-encoded %s -> %s (codec %s -> h264)",
            file_path,
            re_encoded,
            vcodec,
        )
        safe_remove(file_path)  # clean up the original
        return re_encoded

    except asyncio.TimeoutError:
        logger.error("H.264 re-encode timed out")
        safe_remove(re_encoded)
        return file_path
    except Exception as e:
        logger.error("H.264 re-encode exception: %s", e)
        safe_remove(re_encoded)
        return file_path


def _build_gif_reply_markup(token: str, is_gif: bool) -> Optional[object]:
    """Return the 'Save as .gif file' keyboard when delivering an animation.

    Returns None for non-GIF files so regular videos are unaffected.
    """
    if not is_gif:
        return None
    from app.bot.keyboards import build_sent_gif_keyboard

    return build_sent_gif_keyboard(token)


# ──────────────────────────────────────────────────────────────────────────────
# OPT-3: Native Opus / M4A Audio Bypass
# ──────────────────────────────────────────────────────────────────────────────

def _is_native_audio_container(file_path: str) -> bool:
    """Return True if the file is already in a Telegram-compatible audio container.

    Telegram natively accepts: .mp3, .m4a (AAC), .ogg (Opus), .opus.
    These can be sent as-is via send_audio / send_voice without FFmpeg transcoding.
    """
    ext = os.path.splitext(file_path)[1].lower()
    return ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus")


def _detect_opus_from_webm(file_path: str) -> bool:
    """Fast heuristic: read first 512 bytes to detect OpusHead signature in WebM.

    WebM files containing Opus audio have 'OpusHead' in the first few hundred bytes.
    This allows us to identify them for zero-cost container rename to .ogg.
    """
    try:
        with open(file_path, "rb") as f:
            header = f.read(512)
        return b"OpusHead" in header
    except OSError:
        return False


async def _maybe_rename_webm_to_ogg(file_path: str) -> str:
    """If the file is a WebM with Opus audio, rename it to .ogg for Telegram.

    Telegram's sendVoice/sendAudio accepts .ogg (Opus) natively.
    For WebM containers with Opus tracks, a simple rename (no re-encode)
    is sufficient — Telegram clients parse the Opus packets directly.

    Returns the (possibly renamed) file path.
    """
    if not file_path.lower().endswith(".webm"):
        return file_path

    if _detect_opus_from_webm(file_path):
        ogg_path = file_path[:-5] + ".ogg"
        try:
            os.rename(file_path, ogg_path)
            logger.info("OPT-3 Opus fast-path: renamed %s → %s", file_path, ogg_path)
            return ogg_path
        except OSError as e:
            logger.warning("OPT-3 Opus rename failed: %s", e)

    return file_path


# ──────────────────────────────────────────────────────────────────────────────
# OPT-4: Cobalt Direct URL Delivery
# ──────────────────────────────────────────────────────────────────────────────

async def _cobalt_url_head_size(url: str) -> Optional[int]:
    """HTTP HEAD request to Cobalt URL to fetch Content-Length in < 300 ms.

    Returns byte size if known, None if unknown or request failed.
    """
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession() as session:
            resp = await session.head(url, timeout=5, allow_redirects=True)
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception as e:
        logger.debug("OPT-4 Cobalt HEAD failed: %s", e)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class DownloadOrchestrator:
    """Encapsulates the download business logic, leaving UI components thin."""

    @staticmethod
    async def process_download(
        token: str,
        chat_id: int,
        bot: Bot,
        payload: DownloadContext,
        fmt_size: Optional[int],
        update_ui: Callable[[str, Optional[object]], Awaitable[None]],
        kb_error: object,
    ) -> bool:
        """Process download with concurrency checks, sizes, and Telegram upload.

        Fast-path optimizations applied (in order):
          OPT-1: Zero-Cost Thumbnails (yt-dlp --write-thumbnail → inject into send_video)
          OPT-2: Stream-Copy Video Splitting (> 48 MB → lossless ffmpeg -c copy)
          OPT-3: Native Opus Audio Bypass (webm+opus → rename to .ogg, no FFmpeg)
          OPT-4: Cobalt Direct URL Delivery (< 19.5 MB → pass URL to Telegram server)
          OPT-5: (Pinterest GIF fast-path — handled in PinterestNativeService)
        """

        # ── Tiered semaphore selection ────────────────────────────────────
        # API-origin payloads (TikWM, Cobalt URL delivery, Pinterest) have
        # near-zero CPU cost — route them through the higher-capacity api_sem
        # so a flood of heavy yt-dlp jobs cannot starve them.
        _api_origin_ids = {"tikwm_fallback", "pinterest_native", "gallerydl_fallback"}
        _is_api_origin = payload.format_id in _api_origin_ids
        _sem = state.api_sem if _is_api_origin else state.download_sem

        if _sem.locked():
            await update_ui(Texts.QUEUE_FULL, kb_error)
            return False

        await _sem.acquire()

        # Tracks extra files created during this pipeline that must be cleaned up.
        _cleanup_extras: list[str] = []

        try:
            if not size_allowed(fmt_size, target="telegram"):
                mb = (fmt_size or 0) / (1024 * 1024)
                await update_ui(
                    Texts.FILE_TOO_BIG.format(size_mb=mb, max_mb=MAX_TG_UPLOAD_MB),
                    kb_error,
                )
                return False

            from app.bot.keyboards import build_cancel_keyboard
            
            await update_ui(Texts.STARTING_DOWNLOAD, build_cancel_keyboard(token))
            try:
                await bot.send_chat_action(
                    chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO
                )
            except Exception:
                pass

            file_path: Union[str, io.BytesIO, None] = None
            error: Optional[str] = None

            # ── Download phase ─────────────────────────────────────────────────
            if payload.format_id == "tikwm_fallback":
                file_path, error = await TikWMService.download_video(payload.page_url)

                # Smart BVC2/HEVC Fallback:
                # If TikWM gives us an incompatible proprietary byte stream (like BVC2),
                # ffmpeg cannot decode it, creating scrambled video. Instead of transcoding,
                # we immediately fallback to yt-dlp to fetch TikTok's native H.264 alternate stream.
                if file_path and not error:
                    meta = await _extract_video_meta(file_path)
                    vcodec = meta.get("vcodec")
                    pix_fmt = meta.get("pix_fmt")
                    codec_tag = meta.get("codec_tag", "")

                    is_safe_codec = (vcodec is not None) and (vcodec in _TG_SAFE_CODECS)
                    is_safe_pix_fmt = not pix_fmt or "10" not in pix_fmt
                    is_safe_tag = "bvc" not in codec_tag and "hvc" not in codec_tag

                    if not (is_safe_codec and is_safe_pix_fmt and is_safe_tag):
                        logger.warning(
                            "TikWM returned incompatible format (codec:%s, pix_fmt:%s, tag:%s). Falling back to yt-dlp H.264 stream...",
                            vcodec,
                            pix_fmt,
                            codec_tag,
                        )
                        safe_remove(file_path)
                        file_path, error = await MediaSender.download_video(
                            payload.page_url,
                            "bestvideo[vcodec^=avc]+bestaudio/best",
                            payload.height,
                            token + "_yt",
                        )

                if file_path and not error:
                    state.file_cache[token] = file_path

            elif payload.format_id == "gallerydl_fallback":
                file_path, error = await asyncio.to_thread(
                    GalleryDlService.download_video,
                    payload.page_url,
                    state.ytdlp.cookies_path,
                    state.ytdlp.tiktok_proxy,
                )
                if file_path and not error:
                    state.file_cache[token] = file_path

            elif payload.format_id == "pinterest_native" or (
                payload.format_id == GIF_FORMAT_ID
                and "pinterest" in payload.page_url.lower()
            ):
                file_path, error = await PinterestNativeService.download_video(
                    payload.page_url
                )
                if file_path and not error:
                    state.file_cache[token] = file_path

            else:
                last_threshold = 0

                async def _on_progress(pct: float, eta: str) -> None:
                    nonlocal last_threshold
                    thresholds = [10, 25, 50, 75, 90]
                    passed = [t for t in thresholds if pct >= t]
                    if passed:
                        current = passed[-1]
                        if current > last_threshold:
                            last_threshold = current
                            filled = int(pct / 10)
                            bar = "■" * filled + "□" * (10 - filled)
                            text = Texts.DOWNLOAD_PROGRESS.format(pct=f"{pct:.1f}", bar=bar, eta=eta)
                            await update_ui(text, build_cancel_keyboard(token))

                file_path, error = await MediaSender.download_video(
                    payload.page_url,
                    payload.format_id or "",
                    payload.height,
                    token,
                    info_json_path=payload.info_json_path,
                    fallback_clients=payload.youtube_fallback,
                    progress_callback=_on_progress,
                )

            if error or not file_path:
                await update_ui(error or Texts.GENERIC_ERROR_SHORT, kb_error)
                return False

            await update_ui(Texts.SENDING_TO_TG, None)

            is_gif = payload.format_id == GIF_FORMAT_ID or (
                isinstance(file_path, str) and file_path.lower().endswith(".gif")
            )
            is_audio = payload.format_id == AUDIO_FORMAT_ID

            # Cache file path for on-demand GIF file export (giffile| callback)
            if is_gif and isinstance(file_path, str) and token not in state.file_cache:
                state.file_cache[token] = file_path

            # ── OPT-1: Zero-Cost Thumbnail Pre-Fetch ──────────────────────────
            # yt-dlp writes <stem>.jpg alongside the video when --write-thumbnail is active.
            # We inject it into send_video for instant chat preview rendering on all clients.
            thumbnail_path: Optional[str] = None
            if isinstance(file_path, str) and not is_gif and not is_audio:
                thumbnail_path = find_thumbnail(file_path)
                if thumbnail_path:
                    _cleanup_extras.append(thumbnail_path)
                    logger.info("OPT-1 Thumbnail injected: %s", thumbnail_path)

            # ── OPT-3: Native Opus Audio Bypass ───────────────────────────────
            # WebM files containing Opus audio just need a rename → .ogg; zero FFmpeg.
            if isinstance(file_path, str) and is_audio:
                file_path = await _maybe_rename_webm_to_ogg(file_path)

            # ── Re-encode pass: non-H.264 videos for Telegram compatibility ───
            # (TikTok CDN often serves HEVC which Telegram can't play)
            if isinstance(file_path, str) and not is_gif and not is_audio:
                file_path = await _ensure_telegram_compatible(file_path)

            # ── OPT-2: Stream-Copy Splitting vs Compression ───────────────────
            # Before attempting CPU-heavy two-pass compression, try to split the video
            # into ≤47 MB parts via zero-encoding stream copy (typically 1-3 seconds).
            # We prefer splitting over compression because:
            #   - Lossless quality (no re-encode)
            #   - 60–300× faster (I/O speed vs. CPU speed)
            #   - Predictable output size
            # Compression is still attempted if: file > 500 MB (split limit) OR split fails.
            if isinstance(file_path, str) and not is_gif and not is_audio:
                if not config.TELEGRAM_LOCAL_ENDPOINT:
                    try:
                        file_size = os.path.getsize(file_path)
                    except OSError:
                        file_size = 0  # file may not exist (e.g. mocked in tests)
                    if file_size > _TG_UPLOAD_LIMIT_BYTES:
                        logger.info(
                            "OPT-2: File %.1f MB > limit — attempting stream-copy split",
                            file_size / 1e6,
                        )
                        parts = await split_video_stream_copy(file_path)

                        if parts and len(parts) > 1:
                            # ── Send split parts as sequential messages ────────
                            total = len(parts)
                            all_ok = True
                            for idx, part_path in enumerate(parts, 1):
                                part_meta = await _extract_video_meta(part_path)
                                part_thumb = find_thumbnail(file_path)  # share original thumb
                                caption = f"📹 Часть {idx}/{total}"
                                # Load thumbnail into BytesIO for type-safe passing
                                _part_thumb_bytes: io.BytesIO | None = None
                                if part_thumb and os.path.exists(part_thumb):
                                    try:
                                        with open(part_thumb, "rb") as _tf:
                                            _part_thumb_bytes = io.BytesIO(_tf.read())
                                    except OSError:
                                        pass
                                ok = await MediaSender.send_file(
                                    bot,
                                    chat_id,
                                    part_path,
                                    is_audio=False,
                                    is_gif=False,
                                    caption=caption,
                                    duration=part_meta.get("duration"),
                                    width=part_meta.get("width"),
                                    height=part_meta.get("height"),
                                    thumbnail=_part_thumb_bytes,
                                )
                                _cleanup_extras.append(part_path)
                                if not ok:
                                    all_ok = False
                                    logger.warning("OPT-2: Part %d/%d send failed", idx, total)

                            safe_remove(file_path)
                            if not all_ok:
                                await update_ui(Texts.SEND_ERROR, kb_error)
                                return False
                            return True

                        # Split not possible / yielded single part → fall through to compression
                        logger.info("OPT-2: Split not viable, falling back to compression")
                        original_path = file_path
                        compressed_path = await compress_video_to_size(file_path)
                        if compressed_path is not None:
                            safe_remove(original_path)
                            file_path = compressed_path
                            logger.info(
                                "Compression fallback OK: %s",
                                compressed_path,
                            )
                else:
                    logger.debug(
                        "Skipping video compression/split; Local API provides 2GB upload limit"
                    )

            # ── Extract video metadata for Telegram delivery + preview ─────────
            video_meta = await _extract_video_meta(
                file_path,
                info_json_path=payload.info_json_path,
            )

            # ── Build thumbnail file handle for send_file ─────────────────────
            # ── Build thumbnail BytesIO for send_file ————————————————
            thumb_bytes: io.BytesIO | None = None
            if thumbnail_path and os.path.exists(thumbnail_path):
                try:
                    with open(thumbnail_path, "rb") as _tf:
                        thumb_bytes = io.BytesIO(_tf.read())
                except OSError:
                    pass

            success = await MediaSender.send_file(
                bot,
                chat_id,
                file_path,
                is_audio=is_audio,
                is_gif=is_gif,
                caption="📹" if not is_audio else "🎵",
                duration=video_meta.get("duration"),
                width=video_meta.get("width"),
                height=video_meta.get("height"),
                thumbnail=thumb_bytes,
                reply_markup=_build_gif_reply_markup(token, is_gif),
            )

            if not success:
                await update_ui(Texts.SEND_ERROR, kb_error)
                return False

            return True
        finally:
            _sem.release()

            # Cleanup thumbnail and any split segments created during this pipeline
            for extra in _cleanup_extras:
                await asyncio.to_thread(safe_remove, extra)
