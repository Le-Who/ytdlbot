"""Константы приложения"""

CHUNK_SIZE = 1024 * 1024


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
    "instagram.com",
    "instagr.am",
    "www.instagram.com",
    "x.com",
    "twitter.com",
}
SUPPORTED_PLATFORMS_SUFFIXES = tuple(f".{p}" for p in SUPPORTED_PLATFORMS)

VIDEO_EXTENSIONS = {"mp4", "webm", "3gp"}
AUDIO_FORMAT_ID = "bestaudio/best"
GIF_FORMAT_ID = "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best"

SLIDESHOW_PHOTO_FORMAT_ID = "__slideshow_photos__"
SLIDESHOW_VIDEO_FORMAT_ID = "__slideshow_video__"

HEIGHT_PATTERN = r"(\d+)p"
