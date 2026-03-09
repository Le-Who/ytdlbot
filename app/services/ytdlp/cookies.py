import os
import tempfile
import base64
import logging
import atexit
from typing import Optional

logger = logging.getLogger("ytdlp_service.cookies")


class PlatformCookiesManager:
    """Per-platform cookie file management.

    Architecture:
      - YTDLP_COOKIES_B64       → global fallback (YouTube, VK, etc.)
      - TIKTOK_COOKIES_B64      → override for TikTok
      - FACEBOOK_COOKIES_B64    → override for Facebook

    Priority: platform-specific → global fallback.
    """

    # platform keyword (matched against URL) → env var name
    _PLATFORM_ENV_MAP: dict[str, str] = {
        "tiktok.com": "TIKTOK_COOKIES_B64",
        "facebook.com": "FACEBOOK_COOKIES_B64",
        "fb.watch": "FACEBOOK_COOKIES_B64",
    }

    _GLOBAL_ENV_VAR = "YTDLP_COOKIES_B64"

    def __init__(self) -> None:
        # platform keyword → temp file path (or None)
        self._platform_cookies: dict[str, str] = {}
        self._global_cookies_path: Optional[str] = None
        self._initialize()

    def _initialize(self) -> None:
        """Decode and write all cookie files from env vars."""
        # 1. Global cookies (fallback for all platforms)
        self._global_cookies_path = self._decode_cookies(self._GLOBAL_ENV_VAR, "global")

        # 2. Platform-specific cookies
        # Deduplicate env vars (facebook.com and fb.watch share same var)
        seen_env_vars: dict[str, str] = {}  # env_var → path
        for keyword, env_var in self._PLATFORM_ENV_MAP.items():
            if env_var in seen_env_vars:
                # Reuse already decoded file
                self._platform_cookies[keyword] = seen_env_vars[env_var]
                continue

            path = self._decode_cookies(env_var, keyword)
            if path:
                self._platform_cookies[keyword] = path
                seen_env_vars[env_var] = path

        # Log summary
        configured = [k for k, v in self._platform_cookies.items() if v]
        if configured:
            logger.info("Platform cookies configured: %s", ", ".join(configured))
        if self._global_cookies_path:
            logger.info("Global cookies configured (YTDLP_COOKIES_B64)")

    def get_cookies_path(self, url: str) -> Optional[str]:
        """Return the best cookies file path for the given URL.

        Priority: platform-specific → global fallback → None.
        """
        lower_url = url.lower()
        for keyword, path in self._platform_cookies.items():
            if keyword in lower_url:
                return path

        return self._global_cookies_path

    @property
    def tiktok_cookies_path(self) -> Optional[str]:
        """Shortcut for TikTok cookies (used by gallery-dl, TikWM)."""
        return self._platform_cookies.get("tiktok.com") or self._global_cookies_path

    @staticmethod
    def _decode_cookies(env_var: str, label: str) -> Optional[str]:
        """Decode a base64 env var into a temp cookies file."""
        b64 = os.getenv(env_var, "").strip()
        if not b64:
            return None

        try:
            raw_data = base64.b64decode(b64)

            try:
                content = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("[%s] Cookies not UTF-8, trying cp1252", label)
                try:
                    content = raw_data.decode("cp1252")
                except UnicodeDecodeError:
                    content = raw_data.decode("latin1")

            fd, path = tempfile.mkstemp(prefix=f"cookies_{label}_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)

            logger.info("[%s] Cookies initialized at %s", label, path)
            _log_cookie_diagnostics(content, label)

            atexit.register(_cleanup_file, path)
            return path

        except Exception as e:
            logger.error("[%s] Failed to init cookies: %s", label, e)
            return None


# ── Module-level helpers ──────────────────────────────────────────

_CRITICAL_COOKIES = {
    # TikTok
    "sid_tt",
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "passport_csrf_token",
    "tt_csrf_token",
    "msToken",
    "odin_tt",
    "ttwid",
    # Facebook
    "c_user",
    "xs",
    "datr",
    "fr",
    # YouTube / Google
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
}


def _log_cookie_diagnostics(content: str, label: str) -> None:
    """Log cookie file diagnostics for debugging (no values exposed)."""
    found: dict[str, str] = {}  # name → domain
    total = 0

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            total += 1
            domain, name = parts[0], parts[5]
            if name in _CRITICAL_COOKIES:
                found[name] = domain

    logger.info(
        "[%s] Cookie diagnostics: %d total, %d/%d critical present",
        label,
        total,
        len(found),
        len(_CRITICAL_COOKIES),
    )
    if found:
        logger.info(
            "[%s] Critical cookies found: %s",
            label,
            ", ".join(f"{n}@{d}" for n, d in sorted(found.items())),
        )


def _cleanup_file(path: str) -> None:
    """Remove a temp file at process exit.

    NOTE: This runs via atexit, so logging streams may already be closed.
    All log calls must be guarded against ValueError / OSError.
    """
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass
