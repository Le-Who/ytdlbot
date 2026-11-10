import os
import re
from urllib.parse import urlsplit

from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES

__all__ = [
    "extract_supported_url",
    "extract_url_and_section",
    "extract_url_from_update",
    "is_supported_url",
    "safe_remove",
]

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def is_supported_url(text: str) -> bool:
    if text.startswith("-"):
        return False
    try:
        parsed = urlsplit(text)
        domain = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not domain:
            return False
        return domain in SUPPORTED_PLATFORMS or domain.endswith(
            SUPPORTED_PLATFORMS_SUFFIXES
        )
    except ValueError:
        return False


def extract_supported_url(text: str) -> str | None:
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,!:;)")
        if is_supported_url(url):
            return url
    return None


SECTION_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[- ]\s*(\d{1,2}:\d{2}(?::\d{2})?)"
)


def extract_url_and_section(text: str) -> tuple[str | None, str | None]:
    """Extracts URL and optional timestamp section (*start-end) for slicing."""
    url = extract_supported_url(text)
    return url, _extract_section(text) if url else None


def _extract_section(text: str) -> str | None:
    match = SECTION_RE.search(text)
    if match:
        start, end = match.groups()
        return f"*{start}-{end}"
    return None


def extract_url_from_update(message: object) -> tuple[str | None, str | None]:
    """Find a visible or hidden link in the message before checking its reply.

    Within each text or caption, visible URLs take priority over text_link
    entities. The clip comes from the text or caption that contains the link.
    """
    replied = getattr(message, "reply_to_message", None)
    for source in (message, replied):
        for content_attr, entities_attr in (
            ("text", "entities"),
            ("caption", "caption_entities"),
        ):
            raw = getattr(source, content_attr, None)
            if not isinstance(raw, str) or not raw:
                continue
            url, section = extract_url_and_section(raw)
            if url:
                return url, section
            for entity in getattr(source, entities_attr, None) or ():
                if getattr(entity, "type", None) != "text_link":
                    continue
                url = getattr(entity, "url", None)
                if isinstance(url, str) and is_supported_url(url):
                    return url, _extract_section(raw)

    return None, None


def safe_remove(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
