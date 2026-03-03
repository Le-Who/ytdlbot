import os
import tempfile
import base64
import logging
import atexit
from typing import Optional

logger = logging.getLogger("ytdlp_service.cookies")


class CookiesManager:
    """Управление временным файлом cookies"""

    def __init__(self):
        self.cookies_path: Optional[str] = None
        self._initialize_cookies()

    def _initialize_cookies(self) -> None:
        """Создаёт временный файл cookies из переменной окружения"""
        b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
        if not b64:
            return

        try:
            # Декодируем base64 в байты
            raw_data = base64.b64decode(b64)

            # Пытаемся декодировать байты в строку, используя разные кодировки
            try:
                content = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Cookies content is not UTF-8, falling back to cp1252")
                try:
                    content = raw_data.decode("cp1252")
                except UnicodeDecodeError:
                    logger.warning(
                        "Cookies content is not cp1252, falling back to latin1"
                    )
                    content = raw_data.decode("latin1")

            fd, self.cookies_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")

            # Записываем как гарантированный UTF-8
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)

            logger.info(f"Cookies initialized at {self.cookies_path}")

            # Diagnostic: log cookie domains/names (NOT values) for debugging
            self._log_cookie_diagnostics(content)

            atexit.register(self.cleanup)
        except Exception as e:
            logger.error(f"Failed to initialize cookies: {e}")
            self.cookies_path = None

    @staticmethod
    def _log_cookie_diagnostics(content: str) -> None:
        """Log cookie file diagnostics for debugging (no values exposed)."""
        critical_cookies = {
            "sid_tt", "sessionid", "sessionid_ss", "sid_guard",
            "passport_csrf_token", "tt_csrf_token", "msToken",
            "odin_tt", "ttwid",
        }
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
                if name in critical_cookies:
                    found[name] = domain

        missing = critical_cookies - set(found.keys())
        logger.info(
            "Cookie diagnostics: %d total cookies, %d/%d critical present",
            total, len(found), len(critical_cookies),
        )
        if found:
            logger.info(
                "Critical cookies found: %s",
                ", ".join(f"{n}@{d}" for n, d in sorted(found.items())),
            )
        if missing:
            logger.warning("Critical cookies MISSING: %s", ", ".join(sorted(missing)))

    def cleanup(self) -> None:
        """Удаляет временный файл cookies"""
        if self.cookies_path and os.path.exists(self.cookies_path):
            try:
                os.unlink(self.cookies_path)
                logger.info("Cookies file cleaned up")
            except Exception as e:
                logger.warning(f"Failed to cleanup cookies: {e}")
