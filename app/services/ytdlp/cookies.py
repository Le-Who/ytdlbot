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

            # Санитизация контента (исправление TRUD -> TRUE и т.д.)
            sanitized_lines = []
            for line in content.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    sanitized_lines.append(line)
                    continue

                parts = line.split("\t")
                if len(parts) >= 7:
                    # Поля: domain, flag, path, secure, expiration, name, value
                    # Исправляем поле flag (index 1)
                    if parts[1] not in ("TRUE", "FALSE"):
                        if parts[1] == "TRUD":  # Известная ошибка
                            parts[1] = "TRUE"
                        elif parts[1].upper() == "TRUE":
                            parts[1] = "TRUE"
                        elif parts[1].upper() == "FALSE":
                            parts[1] = "FALSE"
                        # Можно добавить эвристику: если начинается с T -> TRUE

                    # Исправляем поле secure (index 3)
                    if parts[3] not in ("TRUE", "FALSE"):
                        if parts[3].upper() == "TRUE":
                            parts[3] = "TRUE"
                        elif parts[3].upper() == "FALSE":
                            parts[3] = "FALSE"

                    sanitized_lines.append("\t".join(parts))
                else:
                    sanitized_lines.append(line)

            final_content = "\n".join(sanitized_lines)

            fd, self.cookies_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")

            # Записываем как гарантированный UTF-8
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(final_content)
                # Убедимся, что файл заканчивается новой строкой
                if not final_content.endswith("\n"):
                    f.write("\n")

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
