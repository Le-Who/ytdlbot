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


def _format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "??"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class YtDlpService:
    def __init__(self):
        self.cookies_path = _cookies_file_from_env()

    def _base_opts(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 60,
            "retries": 10,
            "force_ipv4": True,
            "legacyserverconnect": True,
            "user_agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        }
        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path
        return opts

    def get_cookies_dict(self) -> Dict[str, str]:
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

    def list_formats(self, url: str, max_items: int = 12) -> Tuple[str, List[FormatItem], FormatItem, str]:
        info = self.extract(url)
        title = info.get("title") or "Видео"
        duration_sec = info.get("duration")
        duration_str = _format_duration(duration_sec)

        formats: List[FormatItem] = []
        raw_formats = info.get("formats", [])
        
        is_tiktok = "tiktok.com" in url

        for f in raw_formats:
            # TikTok: иногда без кодеков, но валидные
            if f.get("vcodec") == "none" and not is_tiktok:
                continue
            
            ext = f.get("ext")
            protocol = f.get("protocol") or ""
            
            if ext not in ["mp4", "webm"] and "m3u8" not in protocol:
                 continue

            fid = f.get("format_id")
            if not fid:
                continue

            height = f.get("height")
            if not height:
                note = f.get("format_note", "")
                if "p" in note:
                     try:
                         height = int(note.replace("p", ""))
                     except:
                         pass
                elif is_tiktok:
                    height = 720

            # --- Логика определения размера ---
            fs = f.get("filesize") # Точный размер
            if not fs:
                fs = f.get("filesize_approx") # Примерный размер
            
            # Если размера нет, пробуем рассчитать по битрейту (tbr)
            # tbr = total bit rate (kbit/s)
            if not fs and f.get("tbr") and duration_sec:
                tbr = f.get("tbr")
                fs = int((tbr * 1024 / 8) * duration_sec)
            # ----------------------------------
            
            label_parts = []
            
            if is_tiktok:
                label_parts.append("TikTok Video")
            else:
                label_parts.append(f"{height or '??'}p")
                label_parts.append("MP4")
            
            if fs:
                mb = fs / 1024 / 1024
                if mb < 1:
                    label_parts.append(f"({int(fs/1024)} KB)")
                else:
                    label_parts.append(f"({mb:.1f} MB)")
            else:
                # Если совсем никак не узнать размер
                if "m3u8" in protocol:
                     label_parts.append("(~HLS)")
                else:
                     label_parts.append("(?)")

            label = " ".join(label_parts)

            formats.append(
                FormatItem(
                    format_id=fid,
                    label=label,
                    ext="mp4",
                    height=height or 0,
                    filesize=fs,
                )
            )

        formats.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        
        if not is_tiktok:
            unique_formats = []
            seen = set()
            for fmt in formats:
                h = fmt.height
                if h and h not in seen:
                    unique_formats.append(fmt)
                    seen.add(h)
                elif not h:
                    unique_formats.append(fmt)
            final_formats = unique_formats
        else:
            final_formats = formats
        
        final_formats = final_formats[:max_items]

        audio = FormatItem(
            format_id="bestaudio/best",
            label="🎵 Только аудио (best)",
            ext="audio",
            height=None,
            filesize=None,
        )
        return title, final_formats, audio, duration_str

    def get_direct_url(self, page_url: str, format_id: str) -> Tuple[str, Dict[str, str]]:
        # Метод больше не используется в новой логике, но оставим для совместимости
        opts = self._base_opts()
        opts["format"] = format_id
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
        
        if isinstance(info, dict) and info.get("url"):
            return info["url"], info.get("http_headers") or {}
        req = info.get("requested_formats") or []
        if req and req[0].get("url"):
            return req[0]["url"], req[0].get("http_headers") or {}
        raise RuntimeError("Direct URL not found")
