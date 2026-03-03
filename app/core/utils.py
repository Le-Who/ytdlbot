import os
import re
from urllib.parse import urlsplit

from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES

__all__ = ["is_supported_url", "extract_supported_url", "safe_remove"]

URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')


def is_supported_url(text: str) -> bool:
    if text.startswith("-"):
        return False
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
