from app.core.config import MAX_MEDIA_FILE_MB

__all__ = ["DECIMAL_MB", "max_media_file_bytes", "size_allowed"]

DECIMAL_MB = 1_000_000


def max_media_file_bytes(*, limit_mb: int = MAX_MEDIA_FILE_MB) -> int:
    """Return the configured limit using Telegram's decimal-MB definition."""
    if limit_mb <= 0:
        raise ValueError("media file limit must be positive")
    return limit_mb * DECIMAL_MB


def size_allowed(filesize_bytes: int | None, *, target: str = "telegram") -> bool:
    """Apply the one media limit; delivery enforces the cloud degraded cap."""
    if target not in {"telegram", "http"}:
        raise ValueError(f"unsupported size-policy target: {target}")
    if filesize_bytes is None:
        return True
    return 0 <= filesize_bytes <= max_media_file_bytes()
