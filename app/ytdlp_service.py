import base64
import os
import re
import tempfile
import atexit
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import yt_dlp

from .constants import (
    AUDIO_FORMAT_ID,
    VIDEO_EXTENSIONS,
    HEIGHT_PATTERN,
)


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
            
            # Регистрируем удаление при выходе
            atexit.register(self.cleanup)
        except Exception as e:
            print(f"Failed to initialize cookies: {e}")
            self.cookies_path = None
    
    def cleanup(self) -> None:
        """Удаляет временный файл cookies"""
        if self.cookies_path and os.path.exists(self.cookies_path):
            try:
                os.unlink(self.cookies_path)
            except Exception:
                pass


class YtDlpService:
    """Сервис для работы с yt-dlp"""
    
    # Константы класса
    SOCKET_TIMEOUT = 60
    MAX_RETRIES = 10
    USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    BYTES_IN_KB = 1024
    BITS_IN_BYTE = 8
    
    def __init__(self):
        self.cookies_manager = CookiesManager()
        self.cookies_path = self.cookies_manager.cookies_path
    
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
        """Проверяет, является ли URL ссылкой на TikTok"""
        return "tiktok.com" in url.lower()
    
    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        """Форматирует длительность в читаемый вид"""
        if not seconds:
            return "??"
        
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    
    @staticmethod
    def _extract_height(format_note: str) -> Optional[int]:
        """Извлекает высоту из format_note (например, '1080p60' -> 1080)"""
        match = re.search(HEIGHT_PATTERN, format_note)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass
        return None
    
    def _calculate_filesize(
        self,
        format_dict: Dict[str, Any],
        duration_sec: Optional[int]
    ) -> Optional[int]:
        """Вычисляет размер файла (точный, примерный или по битрейту)"""
        # Пробуем точный размер
        fs = format_dict.get("filesize")
        if fs:
            return fs
        
        # Пробуем примерный размер
        fs = format_dict.get("filesize_approx")
        if fs:
            return fs
        
        # Рассчитываем по битрейту
        tbr = format_dict.get("tbr")
        if tbr and duration_sec:
            return int((tbr * self.BYTES_IN_KB / self.BITS_IN_BYTE) * duration_sec)
        
        return None
    
    def _create_format_label(
        self,
        height: Optional[int],
        filesize: Optional[int],
        protocol: str,
        is_tiktok: bool
    ) -> str:
        """Создаёт читаемый лейбл для формата"""
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
    
    def _parse_format(
        self,
        format_dict: Dict[str, Any],
        duration_sec: Optional[int],
        is_tiktok: bool
    ) -> Optional[FormatItem]:
        """Парсит один формат из списка"""
        # Фильтрация: пропускаем чистое аудио (кроме TikTok)
        if format_dict.get("vcodec") == "none" and not is_tiktok:
            return None
        
        # Проверка расширения и протокола
        ext = format_dict.get("ext")
        protocol = format_dict.get("protocol") or ""
        
        if ext not in VIDEO_EXTENSIONS and "m3u8" not in protocol:
            return None
        
        # Получаем ID формата
        fid = format_dict.get("format_id")
        if not fid:
            return None
        
        # Определяем высоту
        height = format_dict.get("height")
        if not height:
            note = format_dict.get("format_note", "")
            height = self._extract_height(note)
            
            if not height and is_tiktok:
                height = 720  # Fallback для TikTok
        
        # Вычисляем размер файла
        filesize = self._calculate_filesize(format_dict, duration_sec)
        
        # Создаём лейбл
        label = self._create_format_label(height, filesize, protocol, is_tiktok)
        
        return FormatItem(
            format_id=fid,
            label=label,
            ext="mp4",
            height=height or 0,
            filesize=filesize,
        )
    
    def _deduplicate_formats(
        self,
        formats: List[FormatItem],
        is_tiktok: bool
    ) -> List[FormatItem]:
        """Удаляет дубликаты по высоте (кроме TikTok)"""
        if is_tiktok:
            return formats
        
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
    
    def list_formats(
        self,
        url: str,
        max_items: int = 12
    ) -> Tuple[str, List[FormatItem], FormatItem, str]:
        """
        Возвращает список доступных форматов видео
        
        Returns:
            (title, video_formats, audio_format, duration_str)
        """
        info = self.extract(url)
        
        title = info.get("title") or "Видео"
        duration_sec = info.get("duration")
        duration_str = self._format_duration(duration_sec)
        
        raw_formats = info.get("formats", [])
        is_tiktok = self._is_tiktok(url)
        
        # Парсим все форматы
        formats: List[FormatItem] = []
        for raw_fmt in raw_formats:
            fmt = self._parse_format(raw_fmt, duration_sec, is_tiktok)
            if fmt:
                formats.append(fmt)
        
        # Сортируем по высоте и размеру
        formats.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        
        # Удаляем дубликаты
        formats = self._deduplicate_formats(formats, is_tiktok)
        
        # Ограничиваем количество
        formats = formats[:max_items]
        
        # Создаём аудио-формат
        audio = FormatItem(
            format_id=AUDIO_FORMAT_ID,
            label="🎵 Только аудио (best)",
            ext="audio",
            height=None,
            filesize=None,
        )
        
        return title, formats, audio, duration_str
