"""Константы приложения"""
from enum import Enum
from typing import Optional

# Настройки скачивания
CHUNK_SIZE = 1024 * 1024  # 1 MB (Оптимизация: меньше вызовов read/write)

MAX_FORMATS_DISPLAY = 6
DOWNLOAD_TIMEOUT_SECONDS = 5  
UPLOAD_TIMEOUT_SECONDS = 600    # 10 минут

# Regex паттерны
HEIGHT_PATTERN = r"(\d+)p"

# --- Platform Detection ---

class Platform(Enum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    PINTEREST = "pinterest"
    VK = "vk"
    RUTUBE = "rutube"
    OTHER = "other"

    @staticmethod
    def detect(url: str) -> "Platform":
        """Определяет платформу по URL."""
        low = url.lower()
        if "youtube.com" in low or "youtu.be" in low:
            return Platform.YOUTUBE
        if "tiktok.com" in low:
            return Platform.TIKTOK
        if "pinterest.com" in low or "pin.it" in low:
            return Platform.PINTEREST
        if "vk.com" in low or "vkvideo.ru" in low:
            return Platform.VK
        if "rutube.ru" in low:
            return Platform.RUTUBE
        return Platform.OTHER


# Поддерживаемые платформы (домены для URL-валидации)
SUPPORTED_PLATFORMS = {
    "youtube.com", "youtu.be",
    "rutube.ru",
    "vk.com", "vkvideo.ru",
    "tiktok.com",
    "pinterest.com", "pin.it",
}
SUPPORTED_PLATFORMS_SUFFIXES = tuple(f".{p}" for p in SUPPORTED_PLATFORMS)

# --- Format Presets (Single Source of Truth) ---

# Форматы видео (в т.ч. для YouTube DASH/HLS)
VIDEO_EXTENSIONS = {"mp4", "webm", "3gp"}

# Audio-only
AUDIO_FORMAT_ID = "bestaudio/best"

# GIF (video without audio, prefer mp4 container)
GIF_FORMAT_ID = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"

# Group mode: best quality under 45MB, prefer mp4
GROUP_VIDEO_FORMAT = (
    "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]"
    "/best[ext=mp4][filesize<45M]"
    "/best[filesize<45M]"
)

# Group mode for Pinterest (simpler, single-stream)
GROUP_PINTEREST_FORMAT = "best[ext=mp4]/best"

# Audio cascade for merge (Original → English → OrigTag → Any)
AUDIO_SELECTOR = (
    "bestaudio[format_note*=original]"
    "/bestaudio[language^=en]"
    "/bestaudio[language^=orig]"
    "/bestaudio"
    "/bestaudio[ext=m4a]"
    "/bestaudio"
)

# Default extraction format (for service.py _base_opts)
DEFAULT_DOWNLOAD_FORMAT = "bestvideo+bestaudio/best"

# Default format sort (prefer MP4/H.264 for Telegram compatibility)
DEFAULT_FORMAT_SORT = ["res:1080", "ext:mp4", "vcodec:h264", "br", "size"]
