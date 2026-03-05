
import shutil
import subprocess
import json
import os
import sys
import logging
from typing import Dict, Any, List, Optional, Tuple

import yt_dlp

__all__ = ["YtDlpService"]

from app.core.config import TIKTOK_PROXY, TEMP_DIR
from .models import FormatItem, FormatMetadata
from .cookies import PlatformCookiesManager
from .builders import build_command
from .parsers import (
    parse_format_metadata,
    create_format_item,
    deduplicate_formats,
    _format_duration,
    get_special_format,
    _is_youtube,
    _is_tiktok,
    detect_tiktok_slideshow,
    classify_tiktok_content,
    classify_tiktok_error,
    TikTokError,
    BITRATE_COEFFICIENT,
)
from .exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
    DirectDownloadReady,
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
        self.cookies_manager = PlatformCookiesManager()
        self.tiktok_proxy = TIKTOK_PROXY
        self.has_aria2 = bool(shutil.which("aria2c"))
        if self.has_aria2:
            logger.info("✅ aria2c found — multi-connection downloads enabled")
        if self.tiktok_proxy:
            logger.info("🔒 TikTok proxy configured: %s", self.tiktok_proxy)

    @property
    def cookies_path(self) -> Optional[str]:
        """Backward-compat: return TikTok cookies (used by slideshow etc.)"""
        return self.cookies_manager.tiktok_cookies_path

    def _base_opts(self, for_list_formats: bool = False, url: str = "") -> Dict[str, Any]:
        """Базовые опции для yt-dlp"""
        # Start with cached immutable options
        opts = self._BASE_OPTS_TEMPLATE.copy()

        # Add mutable/nested structures freshly to ensure independence
        # Enable TikTok's mobile app API extraction path — needed for
        # age-restricted/classified content that the webpage can't access.
        # An empty string uses yt-dlp's built-in defaults for app_name/version/aid.
        opts["extractor_args"] = {
            "tiktok": {
                "app_info": [""],
            },
        }

        # Add dynamic/instance options
        opts["socket_timeout"] = self.SOCKET_TIMEOUT
        opts["retries"] = self.MAX_RETRIES
        opts["user_agent"] = self.USER_AGENT

        if not for_list_formats:
            opts["format_sort"] = ["res:1080", "vcodec:vp9", "br", "size"]
            opts["format"] = (
                "bestvideo+bestaudio/bestvideo+bestaudio/best/bestvideo/best"
            )

        # Pass cookies for any platform that has them configured
        cookies = self.cookies_manager.get_cookies_path(url)
        if cookies:
            opts["cookiefile"] = cookies

        # Proxy only for TikTok (datacenter IP blocks)
        if _is_tiktok(url) and self.tiktok_proxy:
            opts["proxy"] = self.tiktok_proxy

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
            cmd = base_cmd + args + ["--", url]
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

    def _try_gallery_dl_video(
        self, url: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Try downloading TikTok video via gallery-dl (fallback for classified content)."""
        from app.services.gallery_dl.service import GalleryDlService
        return GalleryDlService.download_video(
            url, self.cookies_path, proxy=self.tiktok_proxy,
        )

    @staticmethod
    def _try_tikwm_video(
        url: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Try downloading TikTok video via TikWM third-party API."""
        from app.services.tikwm import TikWMService
        return TikWMService.download_video(url)

    def extract(self, url: str, for_list_formats: bool = False) -> Dict[str, Any]:
        """Извлекает метаданные видео."""
        opts = self._base_opts(for_list_formats=for_list_formats, url=url)

        # Capture yt-dlp warnings/errors via custom logger
        # (quiet=True suppresses them, but they're critical for debugging)
        class _YtdlpLogger:
            def debug(self, msg, *args):
                pass
            def info(self, msg, *args):
                pass
            def warning(self, msg, *args):
                formatted = msg % args if args else msg
                # Known internal retry — not a real warning
                if "Failed to parse JSON" in formatted:
                    logger.debug("yt-dlp: %s", formatted)
                else:
                    logger.warning("yt-dlp: %s", formatted)
            def error(self, msg, *args):
                formatted = msg % args if args else msg
                logger.error("yt-dlp: %s", formatted)

        opts["logger"] = _YtdlpLogger()

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def list_formats(
        self, url: str, max_items: int = 12
    ) -> Tuple[str, List[FormatItem], FormatItem, str, bool, Optional[str]]:
        """Извлекает форматы видео с обработкой ошибок.

        Returns:
            (title, formats, special_format, duration_str, is_slideshow, info_json_path)
            info_json_path: path to cached extraction JSON for --load-info-json reuse.
        """
        info: Optional[Dict[str, Any]] = None
        used_subprocess = False

        # TikTok pre-routing: detect slideshows by URL pattern BEFORE extraction
        if _is_tiktok(url):
            content_type = classify_tiktok_content(url)
            if content_type == "slideshow":
                logger.info("TikTok /photo/ URL, routing to slideshow: %s", url)
                special_format = get_special_format(url)
                return "TikTok Slideshow", [], special_format, "—", True, None

        try:
            info = self.extract(url, for_list_formats=True)
        except Exception as e:
            info, used_subprocess = self._attempt_youtube_fallback(url, used_subprocess)

            if not info:
                error_msg = str(e).lower()

                if _is_tiktok(url):
                    error_class = classify_tiktok_error(error_msg)

                    if error_class == TikTokError.AUTH_REQUIRED:
                        # Try gallery-dl (only if proxy configured)
                        if self.tiktok_proxy:
                            logger.info(
                                "TikTok classified content, trying gallery-dl "
                                "(proxy configured): %s", url
                            )
                            video_path, gdl_err = self._try_gallery_dl_video(url)
                            if video_path:
                                raise DirectDownloadReady(video_path, "TikTok Video")
                            logger.warning(
                                "gallery-dl fallback failed: %s", gdl_err
                            )

                        # TikWM third-party API — works from datacenter IPs
                        logger.info(
                            "TikTok classified content, trying TikWM: %s", url
                        )
                        video_path, twm_err = self._try_tikwm_video(url)
                        if video_path:
                            raise DirectDownloadReady(video_path, "TikTok Video")
                        logger.warning(
                            "TikWM fallback also failed: %s", twm_err
                        )
                        raise AccessDeniedError(
                            "⚠️ Контент с ограниченным доступом. "
                            "Требуется авторизация (cookies могут быть устаревшими)."
                        )

                    elif error_class == TikTokError.SLIDESHOW:
                        logger.info(
                            "TikTok unsupported URL, routing to slideshow: %s", url
                        )
                        special_format = get_special_format(url)
                        return "TikTok Slideshow", [], special_format, "—", True, None

                    elif error_class == TikTokError.FORBIDDEN:
                        raise AccessDeniedError(Texts.SVC_ACCESS_DENIED)

                    elif error_class == TikTokError.NOT_FOUND:
                        raise VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)

                    elif error_class == TikTokError.LIVE:
                        raise LiveStreamError(Texts.SVC_LIVE_NOT_SUPPORTED)

                    else:  # TikTokError.GENERIC
                        logger.info(
                            "TikTok extraction failed (unknown error), "
                            "trying slideshow fallback: %s", url
                        )
                        special_format = get_special_format(url)
                        return "TikTok Slideshow", [], special_format, "—", True, None

                # Non-TikTok error handling
                if "403" in error_msg or "forbidden" in error_msg:
                    raise AccessDeniedError(Texts.SVC_ACCESS_DENIED)
                elif "404" in error_msg or "not found" in error_msg:
                    raise VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)
                elif "log in" in error_msg or "cookies" in error_msg or "sign in" in error_msg:
                    raise AccessDeniedError(
                        "⚠️ Контент с ограниченным доступом. "
                        "Требуется авторизация (cookies могут быть устаревшими)."
                    )
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

        # Detect TikTok slideshow (image carousel with no video formats)
        is_slideshow = detect_tiktok_slideshow(info, url)

        special_format = get_special_format(url)

        # Cache raw extraction info as JSON for download reuse (--load-info-json)
        info_json_path: Optional[str] = None
        if info and _is_youtube(url):
            try:
                import uuid as _uuid
                info_json_path = os.path.join(
                    TEMP_DIR, f"info_{_uuid.uuid4().hex}.json"
                )
                with open(info_json_path, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False)
                logger.info("Cached extraction info: %s", info_json_path)
            except Exception as exc:
                logger.warning("Failed to cache info JSON: %s", exc)
                info_json_path = None

        return title, formats, special_format, duration_str, is_slideshow, info_json_path

    def build_command(
        self,
        page_url: str,
        format_id: str,
        height: Optional[int],
        output: str,
        max_filesize: Optional[int] = None,
        use_aria2: bool = False,
        info_json_path: Optional[str] = None,
    ) -> List[str]:
        """Proxy to functional builder with state injection"""
        cookies = self.cookies_manager.get_cookies_path(page_url)
        is_tiktok = _is_tiktok(page_url)
        return build_command(
            page_url=page_url,
            format_id=format_id,
            height=height,
            output=output,
            cookies_path=cookies,
            max_filesize=max_filesize,
            proxy=self.tiktok_proxy if is_tiktok else None,
            use_aria2=use_aria2,
            has_aria2_installed=self.has_aria2,
            info_json_path=info_json_path,
        )
