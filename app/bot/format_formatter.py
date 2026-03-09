import logging
from app.services.ytdlp.models import FormatItem

logger = logging.getLogger("app.bot.format_formatter")

BYTES_IN_KB = 1024
BYTES_IN_MB = 1024 * 1024


def format_label(item: FormatItem) -> str:
    """
    Creates a user-facing label for a format button.
    Includes icons, descriptions, and file sizes.
    """
    # Special formats
    if item.format_note == "gif":
        return "🎬 Только GIF"
    elif item.format_note == "audio":
        return "🎵 Audio"

    parts = []

    # 1. Icon & Type
    if item.is_tiktok:
        parts.append("🎵 TikTok")
    else:
        if item.height:
            if item.height >= 1080:
                icon = "📺"
            elif item.height >= 720:
                icon = "📹"
            else:
                icon = "📱"
            parts.append(f"{icon} {item.height}p")
        else:
            parts.append("📹 Video")

    # 2. Size / Protocol
    if item.filesize:
        mb = item.filesize / BYTES_IN_MB
        if mb < 1:
            size_str = f"{int(item.filesize / BYTES_IN_KB)} KB"
        else:
            size_str = f"{mb:.1f} MB"
        parts.append(f"• {size_str}")
    elif "m3u8" in item.protocol:
        parts.append("• HLS")

    return " ".join(parts)
