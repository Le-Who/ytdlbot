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
active_processes = set()
processing_gifs = set()  # Set of tokens currently being converted to GIF
conversion_lock = asyncio.Lock()  # Lock for CPU-intensive conversions

# Глобальный объект приложения Telegram (инициализируется в main.py)
bot_app = None


# Кэши
link_cache: TTLCache = TTLCache(maxsize=500, ttl=LINK_TTL_MINUTES * 60)
info_cache: TTLCache = TTLCache(maxsize=200, ttl=600)
cancel_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
inflight_parsing = {}  # url -> asyncio.Event
file_cache: FileTTLCache = FileTTLCache(
    maxsize=100, ttl=3600, on_eviction=safe_remove
)  # token -> file_path

limiter = LimiterRegistry(
    user=TokenBucketLimiter(LIMITER_USER_CAPACITY, LIMITER_USER_REFILL_PER_SEC),
    chat=TokenBucketLimiter(LIMITER_CHAT_CAPACITY, LIMITER_CHAT_REFILL_PER_SEC),
    ip=TokenBucketLimiter(LIMITER_IP_CAPACITY, LIMITER_IP_REFILL_PER_SEC),
    token=TokenBucketLimiter(LIMITER_TOKEN_CAPACITY, LIMITER_TOKEN_REFILL_PER_SEC),
)
