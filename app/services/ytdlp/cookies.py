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
            data = base64.b64decode(b64.encode("utf-8"))
            fd, self.cookies_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")

            with os.fdopen(fd, "wb") as f:
                f.write(data)

            logger.info(f"Cookies initialized at {self.cookies_path}")
            atexit.register(self.cleanup)
        except Exception as e:
            logger.error(f"Failed to initialize cookies: {e}")
            self.cookies_path = None

    def cleanup(self) -> None:
        """Удаляет временный файл cookies"""
        if self.cookies_path and os.path.exists(self.cookies_path):
            try:
                os.unlink(self.cookies_path)
                logger.info("Cookies file cleaned up")
            except Exception as e:
                logger.warning(f"Failed to cleanup cookies: {e}")
