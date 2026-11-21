from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

from app.core.cache import FileTTLCache
from app.core.config import (
    LIMITER_CHAT_CAPACITY,
    LIMITER_CHAT_REFILL_PER_SEC,
    LIMITER_IG_CAPACITY,
    LIMITER_IG_REFILL_PER_SEC,
    LIMITER_IP_CAPACITY,
    LIMITER_IP_REFILL_PER_SEC,
    LIMITER_TOKEN_CAPACITY,
    LIMITER_TOKEN_REFILL_PER_SEC,
    LIMITER_USER_CAPACITY,
    LIMITER_USER_REFILL_PER_SEC,
    LINK_TTL_MINUTES,
    MAX_CONCURRENT_TASKS,
    MAX_QUEUE_SIZE,
    QUEUE_TIMEOUT_SECONDS,
    REDIS_URL,
)
from app.core.download_queue import DownloadQueue
from app.core.limiter import LimiterRegistry
from app.core.storage import MemoryStorage, RedisStorage
from app.core.storage.base import StateStorage
from app.core.utils import safe_remove
from app.services.ytdlp.service import YtDlpService

logger = logging.getLogger("app")

ytdlp = YtDlpService()

api_sem = asyncio.Semaphore(int(os.getenv("MAX_API_TASKS", "10")))
download_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
parsing_sem = asyncio.Semaphore(5)
active_processes_lock = asyncio.Lock()
active_processes: set[asyncio.subprocess.Process] = set()
processing_gifs: set[str] = set()
conversion_sem = asyncio.Semaphore(3)
gif_file_sem = asyncio.Semaphore(2)


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
    avg_task_seconds=15,
)

disk_critical: bool = False


if TYPE_CHECKING:
    from telegram.ext import Application

    from app.core.job_store import JobStore
    from app.services.media.pipeline import MediaPipeline
bot_app: Application | None = None
media_pipeline: MediaPipeline | None = None
job_store: JobStore | None = None

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

prefs_cache: StateStorage
if redis_client:
    prefs_cache = RedisStorage(redis_client, default_ttl=90 * 86400, prefix="prefs")
else:
    prefs_cache = MemoryStorage(maxsize=5000, ttl=90 * 86400)

inflight_parsing: TTLCache = TTLCache(maxsize=100, ttl=600)
file_cache: FileTTLCache = FileTTLCache(
    maxsize=30, ttl=LINK_TTL_MINUTES * 60, on_eviction=safe_remove
)


@dataclass(frozen=True, slots=True)
class _SentMediaSource:
    path: str
    version: tuple[int, int, int, int]


sent_media_sources: TTLCache[tuple[str, str], _SentMediaSource] = TTLCache(
    maxsize=500, ttl=LINK_TTL_MINUTES * 60
)


def _telegram_source_keys(media: Any) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, value)
        for name in ("file_unique_id", "file_id")
        if isinstance(value := getattr(media, name, None), str) and value
    )


def _source_version(path: str) -> tuple[int, int, int, int]:
    status = os.stat(path)
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def remember_sent_media_source(media: Any, path: str | os.PathLike[str]) -> None:
    """Bind a confirmed Telegram identity without adopting its local file."""
    resolved = str(Path(path).resolve(strict=True))
    source = _SentMediaSource(resolved, _source_version(resolved))
    for key in _telegram_source_keys(media):
        sent_media_sources[key] = source


def local_sent_media_source(media: Any) -> str | None:
    """Use only the unchanged source of this exact successful Telegram upload."""
    for key in _telegram_source_keys(media):
        source = sent_media_sources.get(key)
        if source is None:
            continue
        try:
            if _source_version(source.path) == source.version:
                return source.path
        except OSError:
            pass
        sent_media_sources.pop(key, None)
    return None


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
