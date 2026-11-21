import re

from app.core.texts import Texts


class YtDlpError(Exception):
    """Base exception for yt-dlp service errors."""


class VideoNotFoundError(YtDlpError):
    """Raised when the video cannot be found (404)."""


class AccessDeniedError(YtDlpError):
    """Raised when access is denied (403/Forbidden)."""


class LiveStreamError(YtDlpError):
    """Raised when the video is a live stream."""


class ExtractionError(YtDlpError):
    """Raised for generic extraction errors."""


def map_ytdlp_error(stderr_output: str, url: str) -> YtDlpError:
    """Map raw stderr output from yt-dlp to specific domain exceptions.
    Uses regex patterns to be resilient against standard output prefix changes."""

    stderr_lower = stderr_output.lower()

    if re.search(
        r"(?:http error|status code)\s?(?:403|401)|forbidden|access denied|log in|sign in|cookies",
        stderr_lower,
    ):
        return AccessDeniedError(Texts.SVC_ACCESS_DENIED)

    if re.search(
        r"(?:http error|status code)\s?404|not found|unavailable|private video",
        stderr_lower,
    ):
        return VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)

    if (
        re.search(r"is a live stream|this live event", stderr_lower)
        and "available" not in stderr_lower
    ):
        return LiveStreamError(Texts.SVC_LIVE_NOT_SUPPORTED)

    if "format is not available" in stderr_lower:
        return ExtractionError(Texts.SVC_FORMAT_UNAVAILABLE)

    if "badbrowser" in stderr_lower or (
        "vk.com" in url.lower() and "unsupported url" in stderr_lower
    ):
        return AccessDeniedError(
            "🔐 VK аудио требует авторизации.\n"
            "Настройте <code>VK_COOKIES_B64</code> в конфигурации бота."
        )

    if (
        "geo-restricted" in stderr_lower
        or "uploader has not made this video available in your country" in stderr_lower
    ):
        return AccessDeniedError(
            "⚠️ Это видео недоступно в нашей стране (Geo-restricted)."
        )

    clean_error = (
        stderr_output.strip().split("\n")[-1]
        if stderr_output
        else "Unknown yt-dlp error"
    )
    return ExtractionError(Texts.SVC_EXTRACTION_ERROR.format(detail=clean_error[:300]))
