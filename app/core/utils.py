import os
import time
import asyncio
import re
from collections import deque
from urllib.parse import urlsplit
from app.constants import SUPPORTED_PLATFORMS, SUPPORTED_PLATFORMS_SUFFIXES
from app.core.state import active_processes_lock, active_processes, user_rates

# Regex паттерны
URL_RE = re.compile(r"https?://\S+", re.I)
SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
PROGRESS_RE = re.compile(r"(\d+\.\d+)%")
PROGRESS_DETAILS_RE = re.compile(r"at\s+(\S+).*?ETA\s+(\S+)")

# --- PROGRESS BAR CACHE ---
BLOCK_FULL = "█"
BLOCK_EMPTY = "░"
BAR_LENGTH = 15
PROGRESS_BARS = [
    BLOCK_FULL * i + BLOCK_EMPTY * (BAR_LENGTH - i) for i in range(BAR_LENGTH + 1)
]

def render_progressbar(percent: float, length: int = BAR_LENGTH) -> str:
    """Renders a text-based progress bar."""
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
    """Извлекает и валидирует поддерживаемую ссылку из текста."""
    match = URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).rstrip(".,!:;)")
    if is_supported_url(url):
        return url
    return None

def check_rate_limit(user_id: int, limit: int = 5) -> bool:
    """Проверяет лимит запросов пользователя в минуту."""
    now = time.time()
    # Get history of timestamps, filter out old ones (> 60s ago)
    # user_rates stores a list of timestamps for each user
    history = user_rates.get(user_id, [])
    # If for some reason history is not a list (e.g. legacy int), reset it
    if not isinstance(history, list):
        history = []

    history = [t for t in history if now - t < 60]

    if len(history) >= limit:
        # Update cache with cleaned history to prevent it from growing indefinitely
        # even if blocked, but TTLCache might handle expiration.
        # However, to be safe and keep TTL active, we update it.
        user_rates[user_id] = history
        return False

    history.append(now)
    user_rates[user_id] = history
    return True

def safe_remove(path: str) -> None:
    """Удаляет файл, игнорируя ошибки если файл не найден"""
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

def rename_if_exists(src: str, dst: str) -> None:
    """Переименовывает файл если он существует"""
    if src and os.path.exists(src):
        os.rename(src, dst)

async def run_subprocess(cmd: list, collect_stderr: bool = True):
    """Стандартизированный запуск subprocess с отслеживанием и очисткой"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=(
            asyncio.subprocess.PIPE if collect_stderr else asyncio.subprocess.DEVNULL
        ),
    )

    async with active_processes_lock:
        active_processes.add(proc)

    stderr_data = deque(maxlen=100)
    stderr_task = None

    if collect_stderr:
        async def consume_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_data.append(line)

        stderr_task = asyncio.create_task(consume_stderr())

    try:
        yield proc, stderr_data
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except:
                try:
                    proc.kill()
                except:
                    pass
        await proc.wait()
        if stderr_task:
            await stderr_task
        async with active_processes_lock:
            active_processes.discard(proc)
