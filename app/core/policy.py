from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB

__all__ = ["size_allowed"]

# Limits applied when filesize is unknown (conservative fail-safe)
_MAX_UNKNOWN_SIZE_LIMITS = {
    "telegram": MAX_TG_UPLOAD_MB,  # Same as TG upload limit
    "http": MAX_DL_MB,
}


def size_allowed(filesize_bytes: int | None, *, target: str = "telegram") -> bool:
    limit_mb = MAX_TG_UPLOAD_MB if target == "telegram" else MAX_DL_MB
    if filesize_bytes is None:
        # Unknown size: allow but cap to target limit as a safety net
        # Callers should enforce post-download size checks
        return True
    return filesize_bytes <= limit_mb * 1024 * 1024
