import os
import re
from urllib.parse import urlsplit

from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES
from app.core.config import MAX_TG_UPLOAD_MB, MAX_DL_MB

URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
PROGRESS_RE = re.compile(r"(\d+\.\d+)%")
PROGRESS_DETAILS_RE = re.compile(r"at\s+(\S+).*?ETA\s+(\S+)")

BLOCK_FULL = "█"
BLOCK_EMPTY = "░"
BAR_LENGTH = 15
PROGRESS_BARS = [BLOCK_FULL * i + BLOCK_EMPTY * (BAR_LENGTH - i) for i in range(BAR_LENGTH + 1)]


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
        return domain in SUPPORTED_PLATFORMS or domain.endswith(SUPPORTED_PLATFORMS_SUFFIXES)
    except Exception:
        return False


def extract_supported_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,!:;)")
    if is_supported_url(url):
        return url
    return None


def check_rate_limit(user_id: int | None = None, *, chat_id: int | None = None, ip: str | None = None, limit: int = 5) -> bool:
    from app.core import state

    # keep compatibility with `limit` arg by treating it as single-call cost baseline
    cost = max(1.0, 10.0 / float(limit))
    allowed = True
    if user_id is not None:
        allowed = allowed and state.user_limiter.allow(f"u:{user_id}", cost=cost)
    if chat_id is not None:
        allowed = allowed and state.chat_limiter.allow(f"c:{chat_id}", cost=1.0)
    if ip:
        allowed = allowed and state.ip_limiter.allow(f"ip:{ip}", cost=1.0)
    return allowed


def safe_remove(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def rename_if_exists(src: str, dst: str) -> None:
    if src and os.path.exists(src):
        os.rename(src, dst)


def size_allowed(filesize_bytes: int | None) -> tuple[bool, str | None]:
    if not filesize_bytes:
        return True, None
    size_mb = filesize_bytes / (1024 * 1024)
    if size_mb > MAX_DL_MB:
        return False, f"⚠️ Файл слишком большой для скачивания (>{MAX_DL_MB} МБ)."
    if size_mb > MAX_TG_UPLOAD_MB:
        return False, f"⚠️ Файл слишком большой для отправки в Telegram (>{MAX_TG_UPLOAD_MB} МБ)."
    return True, None


async def run_subprocess(cmd: list, collect_stderr: bool = True):
    from app.core.process import run_subprocess as _run_subprocess

    async with _run_subprocess(cmd, stderr_pipe=collect_stderr) as handle:
        yield handle.proc, handle.stderr_buffer
