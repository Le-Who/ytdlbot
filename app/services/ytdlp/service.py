import shutil
import subprocess
import json
import sys
import logging
from typing import Dict, Any, List, Optional, Tuple

import yt_dlp

__all__ = ["YtDlpService"]

from .models import FormatItem, FormatMetadata
from .cookies import CookiesManager
from .builders import build_command
from .parsers import (
    parse_format_metadata,
    create_format_item,
    deduplicate_formats,
    _format_duration,
    get_special_format,
    _is_youtube,
    _is_tiktok,
    BITRATE_COEFFICIENT,
)
from .exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
)
from app.core.texts import Texts

logger = logging.getLogger("ytdlp_service")


class YtDlpService:
    """Сервис для работы с yt-dlp (Facade)"""

    SOCKET_TIMEOUT = 30
    MAX_RETRIES = 5
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"

    _BASE_OPTS_TEMPLATE: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "force_ipv4": True,
        "legacyserverconnect": True,
        "prefer_free_formats": False,
        "geo_bypass": True,
        "ignoreconfig": True,
        "noprogress": True,
        "no_mtime": True,
        "concurrent_fragment_downloads": 5,
        "hls_use_mpegts": True,
    }

    def __init__(self):
        self.cookies_manager = CookiesManager()
        self.has_aria2 = shutil.which("aria2c") is not None
        if self.has_aria2:
            logger.info("🚀 Aria2c detected! Download acceleration enabled.")
        else:
            logger.info("⚠️ Aria2c not found. Standard download mode.")

    @property
    def cookies_path(self) -> Optional[str]:
        return self.cookies_manager.cookies_path

    def _base_opts(self, for_list_formats: bool = False) -> Dict[str, Any]:
        """Базовые опции для yt-dlp"""
        # Start with cached immutable options
        opts = self._BASE_OPTS_TEMPLATE.copy()

        # Add mutable/nested structures freshly to ensure independence
        opts["extractor_args"] = {}

        # Add dynamic/instance options
        opts["socket_timeout"] = self.SOCKET_TIMEOUT
        opts["retries"] = self.MAX_RETRIES
        opts["user_agent"] = self.USER_AGENT

        if not for_list_formats:
            opts["format_sort"] = ["res:1080", "vcodec:vp9", "br", "size"]
            opts["format"] = (
                "bestvideo+bestaudio/bestvideo+bestaudio/best/bestvideo/best"
            )

        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path

        return opts

    def _extract_youtube_via_subprocess(self, url: str) -> Optional[Dict[str, Any]]:
        """Извлечение через yt-dlp CLI для YouTube — надёжный обход ошибок API."""
        base_cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-json",
            "--no-download",
            "--no-warnings",
            "--no-playlist",
            "--force-ipv4",
            "--geo-bypass",
            "--ignore-config",
        ]
        if self.cookies_path:
            base_cmd.extend(["--cookies", self.cookies_path])

        arg_variants = [
            ["--extractor-args", "youtube:player_client=ios"],
            ["--extractor-args", "youtube:player_client=android"],
            ["--extractor-args", "youtube:player_client=web"],
            [],
        ]

        for args in arg_variants:
            cmd = base_cmd + args + [url]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    return json.loads(result.stdout)
                else:
                    logger.debug(
                        f"Subprocess attempt failed (args={args}), retcode={result.returncode}"
                    )
            except Exception as e:
                logger.debug(
                    f"YouTube subprocess fallback exception (args={args}): {e}"
                )

        return None

    def _attempt_youtube_fallback(
        self, url: str, used_subprocess: bool
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        Attempts to fetch info via subprocess if allowed (YouTube) and needed (not already used).
        Returns (info, new_used_subprocess_state).
        """
        if not used_subprocess and _is_youtube(url):
            logger.info("Attempting YouTube subprocess fallback...")
            info = self._extract_youtube_via_subprocess(url)
            return info, True
        return None, used_subprocess

    def extract(self, url: str, for_list_formats: bool = False) -> Dict[str, Any]:
        """Извлекает метаданные видео."""
        opts = self._base_opts(for_list_formats=for_list_formats)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def list_formats(
        self, url: str, max_items: int = 12
    ) -> Tuple[str, List[FormatItem], FormatItem, str]:
        """Извлекает форматы видео с обработкой ошибок"""
        info: Optional[Dict[str, Any]] = None
        used_subprocess = False
        try:
            info = self.extract(url, for_list_formats=True)
        except Exception as e:
            info, used_subprocess = self._attempt_youtube_fallback(url, used_subprocess)

            if not info:
                error_msg = str(e).lower()
                if "403" in error_msg or "forbidden" in error_msg:
                    raise AccessDeniedError(Texts.SVC_ACCESS_DENIED)
                elif "404" in error_msg or "not found" in error_msg:
                    raise VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)
                elif "live" in error_msg and "available" not in error_msg:
                    raise LiveStreamError(Texts.SVC_LIVE_NOT_SUPPORTED)
                else:
                    logger.error("YtDlp Extraction Error: %s", e, exc_info=True)
                    msg = str(e)
                    if "format is not available" in msg.lower():
                        msg = Texts.SVC_FORMAT_UNAVAILABLE
                    raise ExtractionError(Texts.SVC_EXTRACTION_ERROR.format(detail=msg[:300]))

        if info.get("is_live") or info.get("live_status") == "is_live":
            raise LiveStreamError(
                "⚠️ Это прямая трансляция. Загрузка активных стримов не поддерживается."
            )

        title = info.get("title") or Texts.SVC_DEFAULT_TITLE
        duration_sec = info.get("duration")
        duration_str = _format_duration(duration_sec)

        duration_factor = None
        if duration_sec:
            try:
                duration_factor = float(duration_sec) * BITRATE_COEFFICIENT
            except (ValueError, TypeError):
                duration_factor = None

        raw_formats = info.get("formats", [])
        if not raw_formats:
            info2, used_subprocess = self._attempt_youtube_fallback(
                url, used_subprocess
            )
            if info2:
                raw_formats = info2.get("formats", [])

        is_tiktok_url = _is_tiktok(url)
        formats_meta: List[FormatMetadata] = []
        for raw_fmt in raw_formats:
            fmt = parse_format_metadata(raw_fmt, duration_factor, is_tiktok_url)
            if fmt:
                formats_meta.append(fmt)

        if not formats_meta:
            info2, used_subprocess = self._attempt_youtube_fallback(
                url, used_subprocess
            )
            if info2:
                for raw_fmt in info2.get("formats", []):
                    fmt = parse_format_metadata(raw_fmt, duration_factor, is_tiktok_url)
                    if fmt:
                        formats_meta.append(fmt)

        formats_meta.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        formats_meta = deduplicate_formats(formats_meta, is_tiktok_url)
        formats_meta = formats_meta[:max_items]

        formats = [create_format_item(f, is_tiktok_url) for f in formats_meta]

        special_format = get_special_format(url)
        return title, formats, special_format, duration_str

    def build_command(
        self,
        page_url: str,
        format_id: str,
        height: Optional[int],
        output: str,
        max_filesize: Optional[int] = None,
        use_aria2: bool = False,
    ) -> List[str]:
        """Proxy to functional builder with state injection"""
        return build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            cookies_path=self.cookies_path,
            max_filesize=max_filesize,
            use_aria2=use_aria2,
            has_aria2_installed=self.has_aria2,
        )
