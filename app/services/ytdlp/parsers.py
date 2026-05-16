import re
from collections import OrderedDict
from enum import Enum
from typing import List, Dict, Any, Optional
from app.constants import (
    VIDEO_EXTENSIONS,
    GIF_FORMAT_ID,
    AUDIO_FORMAT_ID,
    HEIGHT_PATTERN,
)
from .models import FormatItem, FormatMetadata

HEIGHT_REGEX = re.compile(HEIGHT_PATTERN)
BYTES_IN_KB = 1024
BYTES_IN_MB = 1024 * 1024
BITRATE_COEFFICIENT = 128.0  # 1024 / 8

_COMMON_HEIGHTS = {
    "1080p": 1080, "1080p60": 1080, "1080p50": 1080,
    "720p": 720, "720p60": 720, "720p50": 720,
    "480p": 480, "360p": 360, "240p": 240, "144p": 144,
    "tiny": None, "small": None, "medium": None, "large": None
}


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url.lower() or "youtu.be" in url.lower()


def _is_pinterest(url: str) -> bool:
    return "pinterest.com" in url.lower() or "pin.it" in url.lower()


def _is_facebook(url: str) -> bool:
    low = url.lower()
    return "facebook.com" in low or "fb.watch" in low


def _is_vk(url: str) -> bool:
    return "vk.com" in url.lower()


def _is_vk_audio(url: str) -> bool:
    """True for VK audio tracks: vk.com/audio{owner}_{id}[_{hash}]"""
    low = url.lower()
    return "vk.com" in low and "/audio" in low


def _is_vk_video(url: str) -> bool:
    """True for VK video clips: vk.com/video*, vk.com/clip*"""
    low = url.lower()
    return "vk.com" in low and ("/video" in low or "/clip" in low)


# ── TikTok content-type classification ─────────────────────────────


class TikTokError(Enum):
    """Structured classification of TikTok extraction errors."""

    AUTH_REQUIRED = "auth"  # login / cookies / sign in
    SLIDESHOW = "slideshow"  # unsupported url → photo content
    FORBIDDEN = "forbidden"  # 403
    NOT_FOUND = "not_found"  # 404
    LIVE = "live"  # live stream
    GENERIC = "generic"  # everything else


def classify_tiktok_content(url: str) -> str:
    """Classify TikTok content type by URL pattern.

    Returns:
        'slideshow' — /photo/ URLs (yt-dlp can't handle these)
        'video'     — everything else
    """
    if "/photo/" in url.lower():
        return "slideshow"
    return "video"


def classify_tiktok_error(error_msg: str) -> TikTokError:
    """Classify a TikTok extraction error into a structured enum."""
    msg = error_msg.lower()
    if "log in" in msg or "cookies" in msg or "sign in" in msg:
        return TikTokError.AUTH_REQUIRED
    if "not available" in msg or "status code" in msg:
        return TikTokError.AUTH_REQUIRED
    if "unsupported url" in msg:
        return TikTokError.SLIDESHOW
    if "403" in msg or "forbidden" in msg:
        return TikTokError.FORBIDDEN
    if "404" in msg or "not found" in msg:
        return TikTokError.NOT_FOUND
    if "live" in msg and "available" not in msg:
        return TikTokError.LIVE
    return TikTokError.GENERIC


def _format_duration(seconds: Optional[float]) -> str:
    """Форматирует длительность в формат HH:MM:SS или MM:SS"""
    if not seconds:
        return "--:--"
    total_seconds = int(seconds)
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _extract_height(format_note: str) -> Optional[int]:
    if not format_note:
        return None
        
    if format_note in _COMMON_HEIGHTS:
        return _COMMON_HEIGHTS[format_note]
        
    match = HEIGHT_REGEX.search(format_note)
    if match:
        return int(match.group(1))
    return None


def _calculate_filesize(
    format_dict: Dict[str, Any], duration_factor: Optional[float]
) -> Optional[int]:
    """Вычисляет размер файла используя pre-calculated duration factor"""
    fs = format_dict.get("filesize")
    if fs:
        return int(fs)
    fs = format_dict.get("filesize_approx")
    if fs:
        return int(fs)
    tbr = format_dict.get("tbr")
    if tbr and duration_factor:
        return int(float(tbr) * duration_factor)
    return None


def parse_format_metadata(
    format_dict: Dict[str, Any],
    duration_factor: Optional[float],
    is_tiktok_url: bool,
) -> Optional[FormatMetadata]:
    vcodec = format_dict.get("vcodec")
    acodec = format_dict.get("acodec")

    if vcodec == "none" and not is_tiktok_url:
        return None

    vcodec_val = vcodec or "unknown"
    acodec_val = acodec or "unknown"
    ext = format_dict.get("ext")
    protocol = format_dict.get("protocol") or ""
    if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
        # Accept formats without ext if they have a direct URL (e.g. Facebook sd/hd)
        if not format_dict.get("url"):
            return None
        ext = "mp4"

    fid = format_dict.get("format_id")
    if not fid:
        return None

    height = format_dict.get("height")
    if not height:
        note = format_dict.get("format_note", "")
        height = _extract_height(note)
        # Infer height from format_id for Facebook (sd/hd progressive formats)
        if not height and fid:
            fid_lower = fid.lower()
            if fid_lower in ("hd", "hd_src") or "_hd" in fid_lower:
                height = 720
            elif fid_lower in ("sd", "sd_src") or "_sd" in fid_lower:
                height = 360
        if not height and is_tiktok_url:
            height = 720

    filesize = _calculate_filesize(format_dict, duration_factor)

    return FormatMetadata(
        format_id=fid,
        ext="mp4",
        height=height or 0,
        filesize=filesize,
        protocol=protocol,
        vcodec=vcodec_val,
        acodec=acodec_val,
    )


def create_format_item(metadata: FormatMetadata, is_tiktok: bool) -> FormatItem:
    final_format_id = metadata.format_id
    if (
        metadata.vcodec != "unknown"
        and metadata.vcodec != "none"
        and metadata.acodec == "none"
        and not is_tiktok
    ):
        audio_sel = "bestaudio[format_note*=original]/bestaudio[language^=en]/bestaudio[language^=orig]/bestaudio/bestaudio[ext=m4a]/bestaudio"
        final_format_id = f"{metadata.format_id}+({audio_sel})"

    return FormatItem(
        format_id=final_format_id,
        ext=metadata.ext,
        height=metadata.height,
        filesize=metadata.filesize,
        is_tiktok=is_tiktok,
        protocol=metadata.protocol,
    )


def _score_format(f: FormatMetadata) -> int:
    """Score a format for deduplication: higher is preferred."""
    s = 0
    if f.vcodec not in ("none", "unknown") and f.acodec not in ("none", "unknown"):
        s += 1000  # Premuxed is heavily preferred to save CPU and RAM
    vcodec_low = f.vcodec.lower()
    if "avc1" in vcodec_low or "h264" in vcodec_low:
        s += 500  # H264 is the most compatible
    elif "mp4" in vcodec_low:
        s += 300
    elif "vp" in vcodec_low:
        s += 100
    return s


def deduplicate_formats(
    formats: List[FormatMetadata], is_tiktok_url: bool
) -> List[FormatMetadata]:
    if is_tiktok_url:
        # TikTok: dedup by filesize since heights are often identical
        unique = []
        seen_sizes = set()
        for fmt in formats:
            key = fmt.filesize or fmt.format_id
            if key not in seen_sizes:
                unique.append(fmt)
                seen_sizes.add(key)
        return unique

    # Pre-compute original positions once (O(N)) to avoid O(N²) list.index() calls
    # in the sort key below. Using id() is safe because the list elements are the
    # same objects throughout this function's scope.
    pos: dict[int, int] = {id(f): i for i, f in enumerate(formats)}

    groups: OrderedDict[int | None, list[FormatMetadata]] = OrderedDict()
    for fmt in formats:
        h = fmt.height
        if h not in groups:
            groups[h] = []
        groups[h].append(fmt)

    unique_formats = []
    for h, fmts in groups.items():
        if not h:
            unique_formats.extend(fmts)
        else:
            best_fmt = sorted(
                fmts, key=lambda f: (_score_format(f), -pos[id(f)]), reverse=True
            )[0]
            unique_formats.append(best_fmt)

    return unique_formats


def get_special_format(url: str) -> FormatItem:
    if _is_pinterest(url):
        return FormatItem(
            format_id=GIF_FORMAT_ID,
            ext="gif",
            height=None,
            filesize=None,
            format_note="gif",
        )
    else:
        return FormatItem(
            format_id=AUDIO_FORMAT_ID,
            ext="audio",
            height=None,
            filesize=None,
            format_note="audio",
        )


def detect_tiktok_slideshow(info: Dict[str, Any], url: str) -> bool:
    """
    Detects if a TikTok URL is a slideshow (image carousel) rather than a video.

    Heuristic:
    1. URL must be TikTok
    2. No formats with video codec exist (only audio-only or empty)
    3. OR extraction returned an error for a TikTok URL (slideshow not supported by yt-dlp)
    """
    if not _is_tiktok(url):
        return False

    raw_formats = info.get("formats", [])
    if not raw_formats:
        # No formats at all — likely a slideshow that yt-dlp can't handle
        return True

    # Check if all formats are audio-only (vcodec == "none")
    has_video = any(fmt.get("vcodec") not in (None, "none") for fmt in raw_formats)

    return not has_video
