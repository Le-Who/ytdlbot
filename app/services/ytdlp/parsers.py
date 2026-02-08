import re
from typing import List, Dict, Any, Optional
from app.constants import VIDEO_EXTENSIONS, GIF_FORMAT_ID, AUDIO_FORMAT_ID, HEIGHT_PATTERN
from .models import FormatItem, FormatMetadata

HEIGHT_REGEX = re.compile(HEIGHT_PATTERN)
BYTES_IN_KB = 1024
BITS_IN_BYTE = 8
BYTES_IN_MB = 1024 * 1024
BITRATE_COEFFICIENT = 128.0  # 1024 / 8

def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()

def _is_youtube(url: str) -> bool:
    return "youtube.com" in url.lower() or "youtu.be" in url.lower()

def _is_pinterest(url: str) -> bool:
    return "pinterest.com" in url.lower() or "pin.it" in url.lower()

def _format_duration(seconds: Optional[float]) -> str:
    """Форматирует длительность в формат HH:MM:SS или MM:SS"""
    if not seconds:
        return "??"
    total_seconds = int(seconds)
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

def _extract_height(format_note: str) -> Optional[int]:
    match = HEIGHT_REGEX.search(format_note or "")
    if match:
        return int(match.group(1))
    return None

def _calculate_filesize(format_dict: Dict[str, Any], duration_factor: Optional[float]) -> Optional[int]:
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

def _create_format_label(
    height: Optional[int],
    filesize: Optional[int],
    protocol: str,
    is_tiktok: bool,
) -> str:
    parts = []

    # 1. Icon & Type
    if is_tiktok:
        parts.append("🎵 TikTok")
    else:
        if height:
            if height >= 1080:
                icon = "📺"
            elif height >= 720:
                icon = "📹"
            else:
                icon = "📱"
            parts.append(f"{icon} {height}p")
        else:
            parts.append("📹 ???p")

    # 2. Size / Protocol
    if filesize:
        mb = filesize / BYTES_IN_MB
        if mb < 1:
            size_str = f"{int(filesize / BYTES_IN_KB)} KB"
        else:
            size_str = f"{mb:.1f} MB"
        parts.append(f"• {size_str}")
    elif "m3u8" in protocol:
        parts.append("• HLS")
    else:
        parts.append("• ?")

    return " ".join(parts)

def parse_format_metadata(
    format_dict: Dict[str, Any],
    duration_factor: Optional[float],
    is_tiktok_url: bool,
) -> Optional[FormatMetadata]:
    if format_dict.get("vcodec") == "none" and not is_tiktok_url:
        return None
    ext = format_dict.get("ext")
    protocol = format_dict.get("protocol") or ""
    if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
        return None

    fid = format_dict.get("format_id")
    if not fid:
        return None

    height = format_dict.get("height")
    if not height:
        note = format_dict.get("format_note", "")
        height = _extract_height(note)
        if not height and is_tiktok_url:
            height = 720

    filesize = _calculate_filesize(format_dict, duration_factor)

    return FormatMetadata(
        format_id=fid, ext="mp4", height=height or 0, filesize=filesize, protocol=protocol
    )


def create_format_item(metadata: FormatMetadata, is_tiktok: bool) -> FormatItem:
    label = _create_format_label(
        metadata.height, metadata.filesize, metadata.protocol, is_tiktok
    )
    return FormatItem(
        format_id=metadata.format_id,
        label=label,
        ext=metadata.ext,
        height=metadata.height,
        filesize=metadata.filesize,
    )


def deduplicate_formats(
    formats: List[FormatMetadata], is_tiktok_url: bool
) -> List[FormatMetadata]:
    if is_tiktok_url:
        return formats
    unique_formats = []
    seen_heights = set()
    for fmt in formats:
        h = fmt.height
        if h and h not in seen_heights:
            unique_formats.append(fmt)
            seen_heights.add(h)
        elif not h:
            unique_formats.append(fmt)
    return unique_formats

def get_special_format(url: str) -> FormatItem:
    if _is_pinterest(url):
         return FormatItem(
            format_id=GIF_FORMAT_ID,
            label="🎬 Только GIF",
            ext="gif",
            height=None,
            filesize=None,
        )
    else:
        return FormatItem(
            format_id=AUDIO_FORMAT_ID,
            label="🎵 Только аудио",
            ext="audio",
            height=None,
            filesize=None,
        )
