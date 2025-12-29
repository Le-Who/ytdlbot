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

    data = base64.b64decode(b64.encode("utf-8"))
    fd, path = tempfile.mkstemp(prefix="cookies_", suffix=".txt")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class YtDlpService:
    def __init__(self):
        self.cookies_path = _cookies_file_from_env()

    def _base_opts(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 25,
            "retries": 3,
            # Стабильнее для некоторых хостингов/сетей
            "force_ipv4": True,
        }
        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path
        return opts

    def get_cookies_dict(self) -> Dict[str, str]:
        """
        Преобразует cookies.txt (Mozilla/Netscape) в dict, чтобы передать в aiohttp ClientSession.
        """
        if not self.cookies_path:
            return {}
        cj = http.cookiejar.MozillaCookieJar(self.cookies_path)
        cj.load(ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in cj}

    def extract(self, url: str) -> Dict[str, Any]:
        with yt_dlp.YoutubeDL(self._base_opts()) as ydl:
            return ydl.extract_info(url, download=False)

    def list_formats(self, url: str, max_items: int = 8) -> Tuple[str, List[FormatItem], FormatItem]:
        info = self.extract(url)
        title = info.get("title") or "Видео"

        formats: List[FormatItem] = []
        for f in info.get("formats", []):
            if f.get("vcodec") == "none":
                continue
            if f.get("ext") != "mp4":
                continue

            fid = f.get("format_id")
            if not fid:
                continue

            height = f.get("height")
            fs = f.get("filesize") or f.get("filesize_approx")
            label = f"{height or '??'}p MP4"
            if fs:
                label += f" (~{int(fs / 1024 / 1024)} MB)"

            formats.append(
                FormatItem(
                    format_id=fid,
                    label=label,
                    ext="mp4",
                    height=height,
                    filesize=fs,
                )
            )

        formats.sort(key=lambda x: (x.height or 0), reverse=True)
        formats = formats[:max_items]

        audio = FormatItem(
            format_id="bestaudio/best",
            label="Только аудио (best)",
            ext="audio",
            height=None,
            filesize=None,
        )
        return title, formats, audio

    def get_direct_url(self, page_url: str, format_id: str) -> Tuple[str, Dict[str, str]]:
        """
        Возвращает (direct_url, http_headers).
        http_headers важно пробрасывать в реальную загрузку.
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

        raise RuntimeError("Не удалось получить прямую ссылку на медиа для выбранного формата.")
