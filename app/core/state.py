from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
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
)
from app.core.limiter import TokenBucketLimiter, LimiterRegistry
from app.core.utils import safe_remove

from app.services.ytdlp.service import YtDlpService

# Инициализация логгера
logger = logging.getLogger("app")

# Сервисы
ytdlp = YtDlpService()

# Глобальные примитивы синхронизации
tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
parsing_sem = asyncio.Semaphore(5)  # Лимит на одновременный парсинг форматов
active_processes_lock = asyncio.Lock()
active_processes: set[asyncio.subprocess.Process] = set()
processing_gifs: set[str] = set()  # Set of tokens currently being converted to GIF
conversion_sem = asyncio.Semaphore(3)  # Bounded concurrency for CPU-intensive conversions
ytdlp_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ytdlp")  # Dedicated pool for yt-dlp

# Глобальный объект приложения Telegram (инициализируется в main.py)
if TYPE_CHECKING:
    from telegram.ext import Application
bot_app: Application | None = None


# Кэши
# INVARIANT: All cache reads/writes MUST happen from the asyncio event-loop
# thread.  cachetools.TTLCache is NOT thread-safe.  The executor (ytdlp_executor)
# returns data to the caller in the event loop, which writes to cache — safe.
link_cache: TTLCache = TTLCache(maxsize=500, ttl=LINK_TTL_MINUTES * 60)
info_cache: TTLCache = TTLCache(maxsize=200, ttl=600)
cancel_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
inflight_parsing: TTLCache = TTLCache(maxsize=100, ttl=600)  # url -> asyncio.Event (auto-evicts after 10 min)
file_cache: FileTTLCache = FileTTLCache(
    maxsize=100, ttl=3600, on_eviction=safe_remove
)  # token -> file_path

limiter = LimiterRegistry(
    user=TokenBucketLimiter(LIMITER_USER_CAPACITY, LIMITER_USER_REFILL_PER_SEC),
    chat=TokenBucketLimiter(LIMITER_CHAT_CAPACITY, LIMITER_CHAT_REFILL_PER_SEC),
    ip=TokenBucketLimiter(LIMITER_IP_CAPACITY, LIMITER_IP_REFILL_PER_SEC),
    token=TokenBucketLimiter(LIMITER_TOKEN_CAPACITY, LIMITER_TOKEN_REFILL_PER_SEC),
)
