import base64
import os
import re
import tempfile
import atexit
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import yt_dlp

from .constants import (
    AUDIO_FORMAT_ID,
    VIDEO_EXTENSIONS,
    HEIGHT_PATTERN,
)

logger = logging.getLogger("ytdlp_service")

@dataclass
class FormatItem:
    """Представление формата видео"""
    format_id: str
    label: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]


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
            # Валидация base64 перед декодированием
            data = base64.b64decode(b64.encode("utf-8"))
            fd, self.cookies_path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
            
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            
            logger.info(f"Cookies initialized at {self.cookies_path}")
            # Регистрируем удаление при выходе
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


class YtDlpService:
    """Сервис для работы с yt-dlp"""
    
    SOCKET_TIMEOUT = 30 # Уменьшили таймаут
    MAX_RETRIES = 5     # Уменьшили ретраи для скорости
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    def __init__(self):
        self.cookies_manager = CookiesManager()
        
    @property
    def cookies_path(self) -> Optional[str]:
        return self.cookies_manager.cookies_path
    
    def _base_opts(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self.SOCKET_TIMEOUT,
            "retries": self.MAX_RETRIES,
            "force_ipv4": True, # Часто помогает на серверах
            "user_agent": self.USER_AGENT,
        }
        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path
        return opts
    
    def extract(self, url: str) -> Dict[str, Any]:
        """Извлекает метаданные видео"""
        with yt_dlp.YoutubeDL(self._base_opts()) as ydl:
            return ydl.extract_info(url, download=False)
    
    @staticmethod
    def _is_tiktok(url: str) -> bool:
        return "tiktok.com" in url.lower()
    
    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if not seconds: return "??"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
    
    @staticmethod
    def _extract_height(format_note: str) -> Optional[int]:
        match = re.search(HEIGHT_PATTERN, format_note or "")
        return int(match.group(1)) if match else None
    
    def list_formats(self, url: str, max_items: int = 12) -> Tuple[str, List[FormatItem], FormatItem, str]:
        info = self.extract(url)
        title = info.get("title") or "Видео"
        duration_sec = info.get("duration")
        duration_str = self._format_duration(duration_sec)
        
        raw_formats = info.get("formats", [])
        is_tiktok = self._is_tiktok(url)
        
        formats: List[FormatItem] = []
        seen_heights = set()
        
        # Сортировка сырых форматов перед обработкой (оптимизация)
        # Сначала обрабатываем видео с лучшим качеством
        
        for f in raw_formats:
            # Пропускаем аудио-онли (кроме тиктока, там странная структура)
            if f.get("vcodec") == "none" and not is_tiktok:
                continue
                
            ext = f.get("ext")
            protocol = f.get("protocol") or ""
            
            # Фильтр расширений
            if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
                continue
                
            fid = f.get("format_id")
            if not fid: continue
            
            # Определяем высоту
            height = f.get("height")
            if not height:
                height = self._extract_height(f.get("format_note", ""))
            
            if not height and is_tiktok:
                height = 720
                
            # Дедупликация (для не-TikTok)
            # Берем только первое (лучшее по tbr/filesize) видео для каждой высоты
            if not is_tiktok:
                if height in seen_heights and height is not None:
                    continue
                if height:
                    seen_heights.add(height)
            
            # Размер файла
            filesize = f.get("filesize") or f.get("filesize_approx")
            if not filesize and f.get("tbr") and duration_sec:
                 filesize = int((f.get("tbr") * 1024 / 8) * duration_sec)

            # Лейбл
            label_parts = []
            if is_tiktok: label_parts.append("TikTok")
            else: label_parts.append(f"{height or '?'}p")
            
            if filesize:
                mb = filesize / (1024 * 1024)
                label_parts.append(f"({mb:.1f} MB)" if mb >= 1 else f"({int(filesize/1024)} KB)")
            
            label = " ".join(label_parts)
            
            formats.append(FormatItem(
                format_id=fid,
                label=label,
                ext="mp4",
                height=height,
                filesize=filesize
            ))
            
        # Финальная сортировка: высота (desc), размер (desc)
        formats.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        formats = formats[:max_items]
        
        audio = FormatItem("bestaudio/best", "🎵 Только аудио", "audio", None, None)
        
        return title, formats, audio, duration_str
