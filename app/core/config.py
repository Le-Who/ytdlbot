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
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "0").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL:
    BASE_URL = "http://localhost:8000"
