"""Константы приложения"""

# Настройки скачивания
CHUNK_SIZE = 1024 * 1024  # 1 MB (Оптимизация: меньше вызовов read/write)


# Поддерживаемые платформы
# Примечание: Pinterest поддерживается yt-dlp, но могут быть проблемы с 403/404 ошибками
# для приватного контента или при отсутствии необходимых заголовков
SUPPORTED_PLATFORMS = {
    "youtube.com",
    "youtu.be",
    "rutube.ru",
    "vk.com",
    "vkvideo.ru",
    "tiktok.com",
    "pinterest.com",
    "pin.it",
    "facebook.com",
    "fb.watch",
}
SUPPORTED_PLATFORMS_SUFFIXES = tuple(f".{p}" for p in SUPPORTED_PLATFORMS)

# Форматы видео (в т.ч. для YouTube DASH/HLS)
VIDEO_EXTENSIONS = {"mp4", "webm", "3gp"}
AUDIO_FORMAT_ID = "bestaudio/best"
GIF_FORMAT_ID = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"

# Форматы TikTok Slideshow
SLIDESHOW_PHOTO_FORMAT_ID = "__slideshow_photos__"
SLIDESHOW_VIDEO_FORMAT_ID = "__slideshow_video__"

# Regex паттерны
HEIGHT_PATTERN = r"(\d+)p"
