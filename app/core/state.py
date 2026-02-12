import asyncio
import logging
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
)
from app.core.limiter import TokenBucketLimiter
from app.core.utils import safe_remove
from app.services.ytdlp.service import YtDlpService

logger = logging.getLogger("app")

ytdlp = YtDlpService()

tasks_sem = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
parsing_sem = asyncio.Semaphore(5)
active_processes_lock = asyncio.Lock()
active_processes = set()
processing_gifs = set()
conversion_lock = asyncio.Lock()

bot_app = None

link_cache: TTLCache = TTLCache(maxsize=500, ttl=LINK_TTL_MINUTES * 60)
info_cache: TTLCache = TTLCache(maxsize=200, ttl=600)
cancel_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
user_rates: TTLCache = TTLCache(maxsize=500, ttl=60)
inflight_parsing = {}
file_cache: FileTTLCache = FileTTLCache(maxsize=100, ttl=3600, on_eviction=safe_remove)

user_limiter = TokenBucketLimiter(LIMITER_USER_CAPACITY, LIMITER_USER_REFILL_PER_SEC)
chat_limiter = TokenBucketLimiter(LIMITER_CHAT_CAPACITY, LIMITER_CHAT_REFILL_PER_SEC)
ip_limiter = TokenBucketLimiter(LIMITER_IP_CAPACITY, LIMITER_IP_REFILL_PER_SEC)
