"""Константы приложения"""

# Настройки скачивания
CHUNK_SIZE = 1024 * 1024  # 1 MB (Оптимизация: меньше вызовов read/write)

MAX_FORMATS_DISPLAY = 6
DOWNLOAD_TIMEOUT_SECONDS = 5  
UPLOAD_TIMEOUT_SECONDS = 600    # 10 минут

# Поддерживаемые платформы
# Примечание: Pinterest поддерживается yt-dlp, но могут быть проблемы с 403/404 ошибками
# для приватного контента или при отсутствии необходимых заголовков
SUPPORTED_PLATFORMS = {
    "youtube.com", "youtu.be",
    "rutube.ru",
    "vk.com", "vkvideo.ru",
    "tiktok.com",
    "pinterest.com", "pin.it",
}

# Форматы видео
VIDEO_EXTENSIONS = {"mp4", "webm"}
AUDIO_FORMAT_ID = "bestaudio/best"
GIF_FORMAT_ID = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"

# Regex паттерны
HEIGHT_PATTERN = r"(\d+)p"
