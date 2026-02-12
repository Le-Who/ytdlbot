import os
import secrets
import tempfile
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
TEMP_DIR = os.getenv("TMPDIR", tempfile.gettempdir())
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN")
if not TELEGRAM_SECRET_TOKEN:
    TELEGRAM_SECRET_TOKEN = secrets.token_urlsafe(32)

LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "30"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "1").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_DL_MB = int(os.getenv("MAX_DL_MB", "1000"))
GROUP_DEFAULT_TARGET_MB = int(os.getenv("GROUP_DEFAULT_TARGET_MB", "45"))

LIMITER_BACKEND = os.getenv("LIMITER_BACKEND", "memory").strip().lower()
LIMITER_USER_CAPACITY = float(os.getenv("LIMITER_USER_CAPACITY", "10"))
LIMITER_USER_REFILL_PER_SEC = float(os.getenv("LIMITER_USER_REFILL_PER_SEC", "0.5"))
LIMITER_CHAT_CAPACITY = float(os.getenv("LIMITER_CHAT_CAPACITY", "20"))
LIMITER_CHAT_REFILL_PER_SEC = float(os.getenv("LIMITER_CHAT_REFILL_PER_SEC", "1"))
LIMITER_IP_CAPACITY = float(os.getenv("LIMITER_IP_CAPACITY", "10"))
LIMITER_IP_REFILL_PER_SEC = float(os.getenv("LIMITER_IP_REFILL_PER_SEC", "0.5"))

JANITOR_INTERVAL_SEC = int(os.getenv("JANITOR_INTERVAL_SEC", "300"))
MAX_TEMP_AGE_SEC = int(os.getenv("MAX_TEMP_AGE_SEC", "3600"))
DISK_USAGE_HIGH_WATERMARK = float(os.getenv("DISK_USAGE_HIGH_WATERMARK", "0.85"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL:
    BASE_URL = "http://localhost:8000"
