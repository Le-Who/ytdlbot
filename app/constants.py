"""Константы приложения"""

# Настройки скачивания
CHUNK_SIZE = 1024 * 1024  # 1 MB (Оптимизация: меньше вызовов read/write)


# Поддерживаемые платформы
# Примечание: Pinterest поддерживается yt-dlp, но могут быть проблемы с 403/404 ошибками
# для приватного контента или при отсутствии необходимых заголовков

TIKTOK_DOMAINS = ("tiktok.com",)
YOUTUBE_DOMAINS = ("youtube.com", "youtu.be")
PINTEREST_DOMAINS = ("pinterest.com", "pin.it")
VK_DOMAINS = ("vk.com", "vkvideo.ru")
RUTUBE_DOMAINS = ("rutube.ru",)

SUPPORTED_PLATFORMS = {
    *TIKTOK_DOMAINS,
    *YOUTUBE_DOMAINS,
    *PINTEREST_DOMAINS,
    *VK_DOMAINS,
    *RUTUBE_DOMAINS,
}

SUPPORTED_PLATFORMS_SUFFIXES = tuple(f".{p}" for p in SUPPORTED_PLATFORMS)

# Форматы видео (в т.ч. для YouTube DASH/HLS)
VIDEO_EXTENSIONS = {"mp4", "webm", "3gp"}
AUDIO_FORMAT_ID = "bestaudio/best"
GIF_FORMAT_ID = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"

# Regex паттерны
HEIGHT_PATTERN = r"(\d+)p"
