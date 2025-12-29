import base64
import os
import tempfile
import http.cookiejar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp


@dataclass
class FormatItem:
    format_id: str
    label: str
    ext: str
    height: Optional[int]
    filesize: Optional[int]


def _cookies_file_from_env() -> Optional[str]:
    b64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
    if not b64:
        return None

    try:
        data = base64.b64decode(b64.encode("utf-8"))
        fd, path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        return None


class YtDlpService:
    def __init__(self):
        self.cookies_path = _cookies_file_from_env()

    def _base_opts(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Увеличиваем таймаут для медленного RuTube
            "socket_timeout": 60,
            "retries": 10,
            # Стабильнее для некоторых хостингов/сетей
            "force_ipv4": True,
            # Помогает при SSL Handshake ошибках
            "legacyserverconnect": True,
            # Имитируем Android клиент (часто помогает от бот-фильтров)
            "user_agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        }
        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path
        return opts

    def get_cookies_dict(self) -> Dict[str, str]:
        """
        Преобразует cookies.txt в dict (если вдруг понадобится).
        """
        if not self.cookies_path:
            return {}
        try:
            cj = http.cookiejar.MozillaCookieJar(self.cookies_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            return {c.name: c.value for c in cj}
        except Exception:
            return {}

    def extract(self, url: str) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(self._base_opts()) as ydl:
            return ydl.extract_info(url, download=False)

    def list_formats(self, url: str, max_items: int = 12) -> Tuple[str, List[FormatItem], FormatItem]:
        info = self.extract(url)
        title = info.get("title") or "Видео"

        formats: List[FormatItem] = []
        raw_formats = info.get("formats", [])

        seen_heights = set()

        for f in raw_formats:
            # Пропускаем чисто аудио или чисто видео (без кодеков)
            if f.get("vcodec") == "none":
                continue
            
            # Разрешаем mp4, webm и m3u8 (VK часто отдает HLS)
            # При скачивании yt-dlp сам конвертирует их в поток
            ext = f.get("ext")
            protocol = f.get("protocol") or ""
            
            # Фильтруем совсем экзотику, но оставляем m3u8_native / m3u8
            if ext not in ["mp4", "webm"] and "m3u8" not in protocol:
                 continue

            fid = f.get("format_id")
            if not fid:
                continue

            height = f.get("height")
            # Если высота не определена, пробуем угадать из format_note
            if not height:
                note = f.get("format_note", "")
                if "p" in note:
                     try:
                         height = int(note.replace("p", ""))
                     except:
                         pass

            # Размер файла (может не быть для m3u8)
            fs = f.get("filesize") or f.get("filesize_approx")
            
            label_ext = "MP4" # Для пользователя всё будет как MP4
            label = f"{height or '??'}p {label_ext}"
            if fs:
                label += f" (~{int(fs / 1024 / 1024)} MB)"
            elif "m3u8" in protocol:
                 label += " (HLS)"

            # Дедупликация: берем лучший формат для каждого разрешения
            # (обычно yt-dlp сортирует от худшего к лучшему, поэтому перезаписываем)
            # Но нам нужен список. Сделаем проще: добавляем всё, потом сортируем.
            
            formats.append(
                FormatItem(
                    format_id=fid,
                    label=label,
                    ext="mp4",
                    height=height or 0,
                    filesize=fs,
                )
            )

        # Сортируем: сначала по высоте (убывание), потом по размеру (убывание)
        formats.sort(key=lambda x: (x.height, x.filesize or 0), reverse=True)
        
        # Убираем дубликаты разрешений, оставляя лучший вариант (первый после сортировки)
        unique_formats = []
        seen = set()
        for fmt in formats:
            if fmt.height and fmt.height not in seen:
                unique_formats.append(fmt)
                seen.add(fmt.height)
            elif not fmt.height:
                unique_formats.append(fmt)
        
        # Ограничиваем кол-во кнопок
        final_formats = unique_formats[:max_items]

        audio = FormatItem(
            format_id="bestaudio/best",
            label="🎵 Только аудио (best)",
            ext="audio",
            height=None,
            filesize=None,
        )
        return title, final_formats, audio

    def get_direct_url(self, page_url: str, format_id: str) -> Tuple[str, Dict[str, str]]:
        """
        Метод оставлен для совместимости, но в новом main.py 
        мы используем subprocess, поэтому он используется реже.
        """
        opts = self._base_opts()
        opts["format"] = format_id

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)

        if isinstance(info, dict) and info.get("url"):
            return info["url"], info.get("http_headers") or {}

        req = info.get("requested_formats") or []
        if req and req[0].get("url"):
            headers = req[0].get("http_headers") or info.get("http_headers") or {}
            return req[0]["url"], headers

        raise RuntimeError("Не удалось получить прямую ссылку.")
