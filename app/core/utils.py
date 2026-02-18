import os
import re
from urllib.parse import urlsplit

from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES

URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
PROGRESS_RE = re.compile(r"(\d+\.\d+)%")
PROGRESS_DETAILS_RE = re.compile(r"at\s+(\S+).*?ETA\s+(\S+)")

BLOCK_FULL = "█"
BLOCK_EMPTY = "░"
BAR_LENGTH = 15
PROGRESS_BARS = [
    BLOCK_FULL * i + BLOCK_EMPTY * (BAR_LENGTH - i) for i in range(BAR_LENGTH + 1)
]


def render_progressbar(percent: float, length: int = BAR_LENGTH) -> str:
    percent = max(0.0, min(100.0, percent))
    filled_length = int(length * percent // 100)
    if length == BAR_LENGTH:
        bar = PROGRESS_BARS[filled_length]
    else:
        bar = BLOCK_FULL * filled_length + BLOCK_EMPTY * (length - filled_length)
    return f"{bar} {percent:.1f}%"


def is_supported_url(text: str) -> bool:
    try:
        parsed = urlsplit(text)
        domain = parsed.hostname
        if not domain:
            return False
        return domain in SUPPORTED_PLATFORMS or domain.endswith(
            SUPPORTED_PLATFORMS_SUFFIXES
        )
    except Exception:
        return False


def extract_supported_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,!:;)")
    return url if is_supported_url(url) else None


def safe_remove(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def rename_if_exists(src: str, dst: str) -> None:
    if src and os.path.exists(src):
        os.rename(src, dst)
