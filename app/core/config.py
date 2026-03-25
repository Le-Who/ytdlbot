import os
import secrets
import tempfile
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
TEMP_DIR = os.getenv("TMPDIR", tempfile.gettempdir())
os.makedirs(TEMP_DIR, exist_ok=True)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
TELEGRAM_SECRET_TOKEN: str = os.getenv(
    "TELEGRAM_SECRET_TOKEN"
) or secrets.token_urlsafe(32)

LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "30"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "1").strip() == "1"
MAX_TG_UPLOAD_MB = int(os.getenv("MAX_TG_UPLOAD_MB", "45"))
MAX_DL_MB = int(os.getenv("MAX_DL_MB", "1000"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "5"))

LIMITER_USER_CAPACITY = float(os.getenv("LIMITER_USER_CAPACITY", "10"))
LIMITER_USER_REFILL_PER_SEC = float(os.getenv("LIMITER_USER_REFILL_PER_SEC", "0.5"))
LIMITER_CHAT_CAPACITY = float(os.getenv("LIMITER_CHAT_CAPACITY", "20"))
LIMITER_CHAT_REFILL_PER_SEC = float(os.getenv("LIMITER_CHAT_REFILL_PER_SEC", "1"))
LIMITER_IP_CAPACITY = float(os.getenv("LIMITER_IP_CAPACITY", "15"))
LIMITER_IP_REFILL_PER_SEC = float(os.getenv("LIMITER_IP_REFILL_PER_SEC", "1"))
LIMITER_TOKEN_CAPACITY = float(os.getenv("LIMITER_TOKEN_CAPACITY", "3"))
LIMITER_TOKEN_REFILL_PER_SEC = float(os.getenv("LIMITER_TOKEN_REFILL_PER_SEC", "0.25"))

MAX_TEMP_AGE_SECONDS = int(os.getenv("MAX_TEMP_AGE_SECONDS", "3600"))
JANITOR_INTERVAL_SECONDS = int(os.getenv("JANITOR_INTERVAL_SECONDS", "300"))

DL_TIMEOUT_TELEGRAM = int(os.getenv("DL_TIMEOUT_TELEGRAM", "600"))
DL_TIMEOUT_HTTP = int(os.getenv("DL_TIMEOUT_HTTP", "900"))

# yt-dlp concurrent fragment downloads for DASH/HLS streams.
# 5 is conservative (yt-dlp default), 8 is optimal for most CDNs.
# Above 10 risks rate-limiting on YouTube. Configurable for easy tuning.
CONCURRENT_FRAGMENTS = int(os.getenv("YTDLP_CONCURRENT_FRAGMENTS", "8"))
YOUTUBE_PIPE_MODE = os.getenv("YOUTUBE_PIPE_MODE", "0").strip() == "1"

REDIS_URL = os.getenv("REDIS_URL", "").strip()

# TikTok proxy — route TikTok requests through WireGuard/SOCKS5 to bypass
# datacenter IP blocks on age-restricted content.
# Example: socks5://wireguard-proxy:1080
TIKTOK_PROXY: Optional[str] = os.getenv("TIKTOK_PROXY", "").strip() or None

# Cobalt API (Primary TikTok backend) - Supports multiple comma-separated instances for fallback
# NOTE: The public api.cobalt.tools now requires Turnstile JWT auth and cannot be used by bots.
# You MUST set your own instance URL. See README.md for deployment instructions.
COBALT_API_URLS = [
    url.strip() for url in os.getenv("COBALT_API_URL", "").split(",") if url.strip()
]
ENABLE_COBALT_TIKTOK = os.getenv("ENABLE_COBALT_TIKTOK", "0").strip() == "1"

COBALT_API_KEY = os.getenv("COBALT_API_KEY", "")

# Instagram is now fully anonymous via curl_cffi and mobile API forgery
# However, IG_SESSION_B64 can optionally be provided to restore stories/highlights access.
IG_SESSION_B64 = os.getenv("IG_SESSION_B64", "")
IG_PROXY: Optional[str] = os.getenv("IG_PROXY", "").strip() or None

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL:
    BASE_URL = "http://localhost:8000"
