import base64
import os
import re
import tempfile
import atexit
import logging
import shutil
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


class YtDlpService:
    """Сервис для работы с yt-dlp"""
    
    SOCKET_TIMEOUT = 30
    MAX_RETRIES = 5
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    BYTES_IN_KB = 1024
    BITS_IN_BYTE = 8
    
    def __init__(self):
        self.cookies_manager = CookiesManager()
        # Проверяем наличие aria2c в системе
        self.has_aria2 = shutil.which("aria2c") is not None
        if self.has_aria2:
            logger.info("🚀 Aria2c detected! Download acceleration enabled.")
        else:
            logger.info("⚠️ Aria2c not found. Standard download mode.")
        
    @property
    def cookies_path(self) -> Optional[str]:
        return self.cookies_manager.cookies_path
    
    def _base_opts(self) -> Dict[str, Any]:
        """Базовые опции для yt-dlp"""
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self.SOCKET_TIMEOUT,
            "retries": self.MAX_RETRIES,
            "force_ipv4": True,
            "legacyserverconnect": True,
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
    def _format_duration(seconds: Optional[float]) -> str:
        """Форматирует длительность в формат HH:MM:SS или MM:SS
        
        Принимает int или float (yt-dlp может возвращать float для некоторых платформ)
        """
        if not seconds: return "??"
        # Конвертируем в int для корректного форматирования
        total_seconds = int(seconds)
        m, s = divmod(total_seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
    
    @staticmethod
    def _extract_height(format_note: str) -> Optional[int]:
        match = re.search(HEIGHT_PATTERN, format_note or "")
        if match:
            return int(match.group(1))
        return None
    
    def _calculate_filesize(self, format_dict: Dict[str, Any], duration_sec: Optional[float]) -> Optional[int]:
        """Вычисляет размер файла, обрабатывая int и float для duration"""
        fs = format_dict.get("filesize")
        if fs: return int(fs) if isinstance(fs, float) else fs
        fs = format_dict.get("filesize_approx")
        if fs: return int(fs) if isinstance(fs, float) else fs
        tbr = format_dict.get("tbr")
        if tbr and duration_sec:
            # Конвертируем duration в число для расчета
            duration = float(duration_sec) if duration_sec else 0
            return int((float(tbr) * self.BYTES_IN_KB / self.BITS_IN_BYTE) * duration)
        return None
    
    def _create_format_label(self, height: Optional[int], filesize: Optional[int], protocol: str, is_tiktok: bool) -> str:
        label_parts = []
        if is_tiktok:
            label_parts.append("TikTok Video")
        else:
            label_parts.append(f"{height or '??'}p")
            label_parts.append("MP4")
        
        if filesize:
            mb = filesize / self.BYTES_IN_KB / self.BYTES_IN_KB
            if mb < 1:
                label_parts.append(f"({int(filesize / self.BYTES_IN_KB)} KB)")
            else:
                label_parts.append(f"({mb:.1f} MB)")
        elif "m3u8" in protocol:
            label_parts.append("(~HLS)")
        else:
            label_parts.append("(?)")
        return " ".join(label_parts)
    
    def _parse_format(self, format_dict: Dict[str, Any], duration_sec: Optional[float], is_tiktok: bool) -> Optional[FormatItem]:
        if format_dict.get("vcodec") == "none" and not is_tiktok:
            return None
        ext = format_dict.get("ext")
        protocol = format_dict.get("protocol") or ""
        if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
            return None
        
        fid = format_dict.get("format_id")
        if not fid: return None
        
        height = format_dict.get("height")
        if not height:
            note = format_dict.get("format_note", "")
            height = self._extract_height(note)
            if not height and is_tiktok: height = 720
        
        filesize = self._calculate_filesize(format_dict, duration_sec)
        label = self._create_format_label(height, filesize, protocol, is_tiktok)
        
        return FormatItem(format_id=fid, label=label, ext="mp4", height=height or 0, filesize=filesize)
    
    def _deduplicate_formats(self, formats: List[FormatItem], is_tiktok: bool) -> List[FormatItem]:
        if is_tiktok: return formats
        unique_formats = []
        seen_heights = set()
        for fmt in formats:
            h = fmt.height
            if h and h not in seen_heights:
                unique_formats.append(fmt)
                seen_heights.add(h)
            elif not h:
                unique_formats.append(fmt)
        return unique_formats
    
    def list_formats(self, url: str, max_items: int = 12) -> Tuple[str, List[FormatItem], FormatItem, str]:
        """Извлекает форматы видео с обработкой ошибок для разных платформ"""
        try:
            info = self.extract(url)
        except Exception as e:
            # Специальная обработка для Pinterest и других платформ
            error_msg = str(e).lower()
            if "403" in error_msg or "forbidden" in error_msg:
                raise Exception("Доступ запрещен. Возможно, контент приватный или требуется авторизация.")
            elif "404" in error_msg or "not found" in error_msg:
                raise Exception("Видео не найдено. Проверьте ссылку.")
            elif "none" in error_msg or "nonetype" in error_msg:
                raise Exception("Ошибка парсинга данных. Попробуйте позже или используйте другую ссылку.")
            else:
                raise Exception(f"Ошибка извлечения: {str(e)[:200]}")
        
        title = info.get("title") or "Видео"
        # yt-dlp может возвращать duration как int или float
        duration_sec = info.get("duration")
        duration_str = self._format_duration(duration_sec)
        
        raw_formats = info.get("formats", [])
        is_tiktok = self._is_tiktok(url)
        
        formats: List[FormatItem] = []
        for raw_fmt in raw_formats:
            fmt = self._parse_format(raw_fmt, duration_sec, is_tiktok)
            if fmt: formats.append(fmt)
        
        formats.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        formats = self._deduplicate_formats(formats, is_tiktok)
        formats = formats[:max_items]
        
        audio = FormatItem(format_id=AUDIO_FORMAT_ID, label="🎵 Только аудио (best)", ext="audio", height=None, filesize=None)
        return title, formats, audio, duration_str
