import json
import os
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
TEMP_DIR = os.getenv("TMPDIR", tempfile.gettempdir())
os.makedirs(TEMP_DIR, exist_ok=True)
MEDIA_DIR = os.getenv("MEDIA_DIR", os.path.join(TEMP_DIR, "ytdlbot-media"))
os.makedirs(MEDIA_DIR, exist_ok=True)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
APP_RELEASE = os.getenv("APP_RELEASE", "dev").strip() or "dev"
TELEGRAM_SECRET_TOKEN: str = os.getenv(
    "TELEGRAM_SECRET_TOKEN"
) or secrets.token_urlsafe(32)
TELEGRAM_LOCAL_ENDPOINT = os.getenv("TELEGRAM_LOCAL_ENDPOINT", "").strip().rstrip("/")


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


# Production Compose sets this explicitly.  Defaulting to the presence of a
# local endpoint keeps development/cloud-only compatibility while preventing a
# configured Local Bot API from being treated as an optional optimization.
TELEGRAM_LOCAL_REQUIRED = _boolean(
    "TELEGRAM_LOCAL_REQUIRED", bool(TELEGRAM_LOCAL_ENDPOINT)
)

LINK_TTL_MINUTES = int(os.getenv("LINK_TTL_MINUTES", "60"))
ENABLE_TELEGRAM_UPLOAD = os.getenv("ENABLE_TELEGRAM_UPLOAD", "1").strip() == "1"


def _positive_int(name: str, raw: str, *, maximum: int | None = None) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if value <= 0 or (maximum is not None and value > maximum):
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise RuntimeError(f"{name} must be positive{suffix}")
    return value


def _resolve_media_file_limit_mb() -> int:
    primary_raw = os.getenv("MAX_MEDIA_FILE_MB")
    legacy_raw = {
        name: os.getenv(name)
        for name in ("MAX_DL_MB", "MAX_TG_UPLOAD_MB")
        if os.getenv(name) is not None
    }
    legacy = {
        name: _positive_int(name, raw or "", maximum=2000)
        for name, raw in legacy_raw.items()
    }
    if primary_raw is not None:
        primary = _positive_int("MAX_MEDIA_FILE_MB", primary_raw, maximum=2000)
        if any(value != primary for value in legacy.values()):
            raise RuntimeError(
                "conflicting media file limits: MAX_MEDIA_FILE_MB and legacy "
                "MAX_DL_MB/MAX_TG_UPLOAD_MB must match"
            )
        return primary
    if len(set(legacy.values())) > 1:
        raise RuntimeError(
            "conflicting media file limits: legacy MAX_DL_MB and "
            "MAX_TG_UPLOAD_MB must match"
        )
    return next(iter(legacy.values()), 2000)


MAX_MEDIA_FILE_MB = _resolve_media_file_limit_mb()
# Compatibility aliases only; all three names resolve to one effective policy.
MAX_TG_UPLOAD_MB = MAX_MEDIA_FILE_MB
MAX_DL_MB = MAX_MEDIA_FILE_MB
TELEGRAM_CLOUD_MAX_FILE_MB = _positive_int(
    "TELEGRAM_CLOUD_MAX_FILE_MB",
    os.getenv("TELEGRAM_CLOUD_MAX_FILE_MB", "50"),
    maximum=50,
)
TELEGRAM_MEDIA_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_MEDIA_WRITE_TIMEOUT", "1200"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "120"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "120"))
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
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

# YouTube POT (Proof-of-Origin Token) provider — bypasses "Sign in to confirm
# you're not a bot" without requiring YouTube cookies.
# Set to the bgutil HTTP server URL. Empty string disables POT injection.
POT_PROVIDER_URL: Optional[str] = os.getenv("POT_PROVIDER_URL", "").strip() or None

REDIS_URL = os.getenv("REDIS_URL", "").strip()


def parse_proxy_uri(proxy_raw: Optional[str]) -> Optional[str]:
    if not proxy_raw:
        return None
    proxy_raw = proxy_raw.strip()
    if not proxy_raw:
        return None

    # If it already has a scheme, assume it is correctly formatted
    if "://" in proxy_raw:
        return proxy_raw

    parts = proxy_raw.split(":")
    if len(parts) == 4:
        # Format: host:port:user:pass
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    elif len(parts) == 2:
        # Format: host:port
        host, port = parts
        return f"http://{host}:{port}"

    # Fallback to appending http://
    return f"http://{proxy_raw}"


# TikTok proxy — route TikTok requests through WireGuard/SOCKS5 to bypass
# datacenter IP blocks on age-restricted content.
# Example: socks5://wireguard-proxy:1080 or host:port:user:pass
TIKTOK_PROXY: Optional[str] = parse_proxy_uri(os.getenv("TIKTOK_PROXY"))

# VK proxy — residential HTTP/SOCKS5 proxy to bypass VK's datacenter IP block.
# VK redirects datacenter IPs to badbrowser.php even with valid cookies.
# Format: http://user:pass@host:port, socks5://host:port, or host:port:user:pass
VK_PROXY: Optional[str] = parse_proxy_uri(os.getenv("VK_PROXY"))

# Cobalt API (Primary TikTok backend) - Supports multiple comma-separated instances for fallback
# NOTE: The public api.cobalt.tools now requires Turnstile JWT auth and cannot be used by bots.
# You MUST set your own instance URL. See README.md for deployment instructions.
COBALT_API_URLS = [
    url.strip() for url in os.getenv("COBALT_API_URL", "").split(",") if url.strip()
]
ENABLE_COBALT_TIKTOK = os.getenv("ENABLE_COBALT_TIKTOK", "0").strip() == "1"
COBALT_CONTRACT_VERIFIED = os.getenv("COBALT_CONTRACT_VERIFIED", "0").strip() == "1"
COBALT_CAPABILITIES = frozenset(
    capability.strip().lower()
    for capability in os.getenv(
        "COBALT_CAPABILITIES",
        "tiktok,twitter,x,instagram,facebook,pinterest",
    ).split(",")
    if capability.strip()
)
SNAPSAVE_CONTRACT_VERIFIED = os.getenv("SNAPSAVE_CONTRACT_VERIFIED", "0").strip() == "1"

COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()


def _cobalt_origin(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("Cobalt credential origin is malformed") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("Cobalt credential must name one exact HTTP(S) origin")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{scheme}://{authority}"


def resolve_cobalt_api_credentials(
    origins: Sequence[str],
    configured: Mapping[str, str],
    legacy_key: str = "",
) -> dict[str, str]:
    """Bind every Cobalt credential to one normalized exact origin."""
    normalized_origins = tuple(_cobalt_origin(origin) for origin in origins)
    if len(set(normalized_origins)) != len(normalized_origins):
        raise RuntimeError("Cobalt API origins must be unique")
    allowed = set(normalized_origins)
    credentials: dict[str, str] = {}
    for raw_origin, raw_key in configured.items():
        origin = _cobalt_origin(str(raw_origin))
        key = str(raw_key).strip()
        if origin not in allowed:
            raise RuntimeError("Cobalt credential names an unconfigured exact origin")
        if not key:
            raise RuntimeError("Cobalt credential must not be empty")
        if origin in credentials:
            raise RuntimeError("Cobalt credential origin is duplicated")
        credentials[origin] = key
    legacy = legacy_key.strip()
    if legacy:
        if len(normalized_origins) != 1:
            raise RuntimeError(
                "COBALT_API_KEY requires exactly one configured exact origin"
            )
        origin = normalized_origins[0]
        mapped = credentials.get(origin)
        if mapped is not None and mapped != legacy:
            raise RuntimeError("conflicting Cobalt credentials for exact origin")
        credentials[origin] = legacy
    return credentials


def _configured_cobalt_api_keys() -> dict[str, str]:
    raw = os.getenv("COBALT_API_KEYS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("COBALT_API_KEYS must be a JSON object") from error
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise RuntimeError("COBALT_API_KEYS must map exact origins to string keys")
    return value


COBALT_API_KEYS = resolve_cobalt_api_credentials(
    COBALT_API_URLS,
    _configured_cobalt_api_keys(),
    COBALT_API_KEY,
)

LIMITER_IG_CAPACITY = float(os.getenv("LIMITER_IG_CAPACITY", "15"))
LIMITER_IG_REFILL_PER_SEC = float(
    os.getenv("LIMITER_IG_REFILL_PER_SEC", str(15 / 3600))
)

# Instagram session rotation pool. Comma-separated Base64 encoded cookies.
# Fallback to IG_SESSION_B64 for backward compatibility.
IG_SESSIONS_B64 = [
    s.strip()
    for s in os.getenv("IG_SESSIONS_B64", os.getenv("IG_SESSION_B64", "")).split(",")
    if s.strip()
]

# Admin chat ID for error reporting. Set to your Telegram user ID.
# If unset, error reporting to admin is disabled.
_admin_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
ADMIN_CHAT_ID: Optional[int] = (
    int(_admin_raw) if _admin_raw.lstrip("-").isdigit() else None
)

# ── Download Queue ───────────────────────────────────────────────────────────
# Hard cap on how many requests can wait in the queue before rejecting.
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "15"))
# Seconds a task may wait in queue before it's abandoned.
QUEUE_TIMEOUT_SECONDS = int(os.getenv("QUEUE_TIMEOUT_SECONDS", "300"))

# ── Disk Protection ──────────────────────────────────────────────────────────
# Janitor checks free disk space after every cleanup cycle.
# Below WARNING → admin alert only.
# Below CRITICAL → admin alert + aggressive purge + maintenance mode (no new downloads).
DISK_WARNING_PCT = int(os.getenv("DISK_WARNING_PCT", "15"))
DISK_CRITICAL_PCT = int(os.getenv("DISK_CRITICAL_PCT", "5"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BASE_URL:
    BASE_URL = "http://localhost:8000"
