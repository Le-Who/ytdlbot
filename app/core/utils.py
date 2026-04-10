import os
import re
from urllib.parse import urlsplit

from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES

__all__ = ["is_supported_url", "extract_supported_url", "extract_url_and_section", "extract_url_from_update", "safe_remove"]

URL_RE = re.compile(r"https?://\S+", re.I)


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


SECTION_RE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[- ]\s*(\d{1,2}:\d{2}(?::\d{2})?)")

def extract_url_and_section(text: str) -> tuple[str | None, str | None]:
    """Extracts URL and optional timestamp section (*start-end) for slicing."""
    url = extract_supported_url(text)
    if not url:
        return None, None
    m = SECTION_RE.search(text)
    if m:
        start, end = m.groups()
        return url, f"*{start}-{end}"
    return url, None


def extract_url_from_update(message: object) -> tuple[str | None, str | None]:
    """Extract a supported URL from a message or its reply_to_message.

    Priority order:
      1. message.text (or message.caption)
      2. message.reply_to_message.text
      3. message.reply_to_message.caption

    Returns (url, section) exactly as extract_url_and_section() does.
    Accepts any object with .text / .caption / .reply_to_message attributes
    so it can be used with both PTB Message objects and simple test stubs.
    """
    for attr in ("text", "caption"):
        raw = getattr(message, attr, None)
        if isinstance(raw, str) and raw:
            url, section = extract_url_and_section(raw.strip())
            if url:
                return url, section

    replied = getattr(message, "reply_to_message", None)
    if replied is not None:
        for attr in ("text", "caption"):
            raw = getattr(replied, attr, None)
            if isinstance(raw, str) and raw:
                url, section = extract_url_and_section(raw.strip())
                if url:
                    return url, section

    return None, None


def safe_remove(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
