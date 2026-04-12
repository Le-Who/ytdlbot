from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any
from cachetools import TTLCache
from app.core.cache import FileTTLCache
from app.core.config import (
    LINK_TTL_MINUTES,
    MAX_CONCURRENT_TASKS,
    LIMITER_USER_CAPACITY,
    LIMITER_USER_REFILL_PER_SEC,
    LIMITER_CHAT_CAPACITY,
    LIMITER_CHAT_REFILL_PER_SEC,
    LIMITER_IP_CAPACITY,
    LIMITER_IP_REFILL_PER_SEC,
    LIMITER_TOKEN_CAPACITY,
    LIMITER_TOKEN_REFILL_PER_SEC,
    LIMITER_IG_CAPACITY,
    LIMITER_IG_REFILL_PER_SEC,
    REDIS_URL,
    MAX_QUEUE_SIZE,
    QUEUE_TIMEOUT_SECONDS,
)
from app.core.limiter import LimiterRegistry
from app.core.utils import safe_remove

from app.services.ytdlp.service import YtDlpService
from app.core.storage.base import StateStorage
from app.core.storage import MemoryStorage, RedisStorage

# Инициализация логгера
logger = logging.getLogger("app")

# Сервисы
ytdlp = YtDlpService()

# Глобальные примитивы синхронизации
# ── Semaphores —————————————————————————————————————————————————————
# Tiered to prevent heavy FFmpeg jobs from starving lightweight API calls.
#
# api_sem     : TikWM / Cobalt / Pinterest fetches (near-zero CPU).
# download_sem: yt-dlp + aria2 downloads (IO-bound, moderate CPU).
# conversion_sem / gif_file_sem retain their existing limits.
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)   # kept for legacy callbacks.py usage
api_sem = asyncio.Semaphore(int(os.getenv("MAX_API_TASKS", "10")))
download_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
parsing_sem = asyncio.Semaphore(5)  # Лимит на одновременный парсинг форматов
active_processes_lock = asyncio.Lock()
active_processes: set[asyncio.subprocess.Process] = set()
processing_gifs: set[str] = set()  # Set of tokens currently being converted to GIF
conversion_sem = asyncio.Semaphore(
    3
)  # Bounded concurrency for CPU-intensive conversions
gif_file_sem = asyncio.Semaphore(
    2
)  # Bounded concurrency for on-demand native .gif file exports (palette+scale)

# ── Download queues (fair wait-queue wrapping each semaphore tier) ──────────
from app.core.download_queue import DownloadQueue

download_queue = DownloadQueue(
    download_sem,
    max_queue_size=MAX_QUEUE_SIZE,
    timeout_seconds=QUEUE_TIMEOUT_SECONDS,
    avg_task_seconds=45,
)
api_queue = DownloadQueue(
    api_sem,
    max_queue_size=MAX_QUEUE_SIZE,
    timeout_seconds=QUEUE_TIMEOUT_SECONDS,
    avg_task_seconds=15,  # API tasks are much faster
)

# ── System health flags ───────────────────────────────────────────────────────
# Set to True by janitor when free disk space drops below DISK_CRITICAL_PCT.
# When True, all new download enqueue() calls are rejected with MAINTENANCE_MODE.
disk_critical: bool = False


# Глобальный объект приложения Telegram (инициализируется в main.py)
if TYPE_CHECKING:
    from telegram.ext import Application
bot_app: Application | None = None

if REDIS_URL:
    import redis.asyncio as redis

    redis_client: Any = redis.from_url(
        REDIS_URL,
        decode_responses=False,
        max_connections=5,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
else:
    redis_client = None

# Explicitly type hints for state storage variables
link_cache: StateStorage
info_cache: StateStorage
cancel_cache: StateStorage
gifdoc_cache: StateStorage

if redis_client:
    link_cache = RedisStorage(
        redis_client, default_ttl=LINK_TTL_MINUTES * 60, prefix="lnk"
    )
    info_cache = RedisStorage(redis_client, default_ttl=600, prefix="inf")
    cancel_cache = RedisStorage(redis_client, default_ttl=3600, prefix="can")
    gifdoc_cache = RedisStorage(redis_client, default_ttl=604800, prefix="gifdoc")
else:
    link_cache = MemoryStorage(maxsize=500, ttl=LINK_TTL_MINUTES * 60)
    info_cache = MemoryStorage(maxsize=200, ttl=600)
    cancel_cache = MemoryStorage(maxsize=100, ttl=3600)
    gifdoc_cache = MemoryStorage(maxsize=500, ttl=604800)

# Per-user download preferences (format / quality)
prefs_cache: StateStorage
if redis_client:
    prefs_cache = RedisStorage(redis_client, default_ttl=90 * 86400, prefix="prefs")
else:
    prefs_cache = MemoryStorage(maxsize=5000, ttl=90 * 86400)

inflight_parsing: TTLCache = TTLCache(
    maxsize=100, ttl=600
)  # url -> asyncio.Event (auto-evicts after 10 min)
file_cache: FileTTLCache = FileTTLCache(
    maxsize=30, ttl=LINK_TTL_MINUTES * 60, on_eviction=safe_remove
)  # token -> file_path

if redis_client:
    from app.core.limiter import RedisTokenBucketLimiter

    limiter = LimiterRegistry(
        user=RedisTokenBucketLimiter(
            redis_client, LIMITER_USER_CAPACITY, LIMITER_USER_REFILL_PER_SEC
        ),
        chat=RedisTokenBucketLimiter(
            redis_client, LIMITER_CHAT_CAPACITY, LIMITER_CHAT_REFILL_PER_SEC
        ),
        ip=RedisTokenBucketLimiter(
            redis_client, LIMITER_IP_CAPACITY, LIMITER_IP_REFILL_PER_SEC
        ),
        token=RedisTokenBucketLimiter(
            redis_client, LIMITER_TOKEN_CAPACITY, LIMITER_TOKEN_REFILL_PER_SEC
        ),
        ig_fallback=RedisTokenBucketLimiter(
            redis_client, LIMITER_IG_CAPACITY, LIMITER_IG_REFILL_PER_SEC
        ),
    )
else:
    from app.core.limiter import TokenBucketLimiter

    limiter = LimiterRegistry(
        user=TokenBucketLimiter(LIMITER_USER_CAPACITY, LIMITER_USER_REFILL_PER_SEC),
        chat=TokenBucketLimiter(LIMITER_CHAT_CAPACITY, LIMITER_CHAT_REFILL_PER_SEC),
        ip=TokenBucketLimiter(LIMITER_IP_CAPACITY, LIMITER_IP_REFILL_PER_SEC),
        token=TokenBucketLimiter(LIMITER_TOKEN_CAPACITY, LIMITER_TOKEN_REFILL_PER_SEC),
        ig_fallback=TokenBucketLimiter(LIMITER_IG_CAPACITY, LIMITER_IG_REFILL_PER_SEC),
    )
