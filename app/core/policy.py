from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB


def size_allowed(filesize_bytes: int | None, *, target: str = "telegram") -> bool:
    if filesize_bytes is None:
        return True
    limit_mb = MAX_TG_UPLOAD_MB if target == "telegram" else MAX_DL_MB
    return filesize_bytes <= limit_mb * 1024 * 1024
