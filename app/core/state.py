import asyncio
import logging
from cachetools import TTLCache
from app.core.config import LINK_TTL_MINUTES, MAX_CONCURRENT_TASKS
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

# Глобальный объект приложения Telegram (инициализируется в main.py)
bot_app = None

# Кэши
link_cache: TTLCache = TTLCache(maxsize=500, ttl=LINK_TTL_MINUTES * 60)
info_cache: TTLCache = TTLCache(maxsize=200, ttl=600)
cancel_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
user_rates: TTLCache = TTLCache(maxsize=500, ttl=60)
inflight_parsing = {}  # url -> asyncio.Event
file_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)  # token -> file_path
