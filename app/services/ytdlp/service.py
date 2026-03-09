import shutil
import json
import os
import sys
import logging
from typing import Dict, Any, List, Optional, Tuple


__all__ = ["YtDlpService"]

from app.core.config import TIKTOK_PROXY, TEMP_DIR, CONCURRENT_FRAGMENTS
from .models import FormatItem, FormatMetadata, ExtractionResult
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
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
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

    def _base_opts(
        self, for_list_formats: bool = False, url: str = ""
    ) -> Dict[str, Any]:
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

    async def _extract_youtube_via_subprocess(self, url: str) -> Optional[Dict[str, Any]]:
        """Извлечение через yt-dlp CLI для YouTube — надёжный обход ошибок API."""
        from app.core.process import run_subprocess
        
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
                async with run_subprocess(cmd, timeout=60) as handle:
                    stdout, stderr = await handle.proc.communicate()
                    if handle.proc.returncode == 0 and stdout.strip():
                        return json.loads(stdout.decode())  # type: ignore
                    else:
                        logger.debug(
                            f"Subprocess attempt failed (args={args}), retcode={handle.proc.returncode}"
                        )
            except Exception as e:
                logger.debug(
                    f"YouTube subprocess fallback exception (args={args}): {e}"
                )

        return None

    async def _attempt_youtube_fallback(
        self, url: str, used_subprocess: bool
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        Attempts to fetch info via subprocess if allowed (YouTube) and needed (not already used).
        Returns (info, new_used_subprocess_state).
        """
        if not used_subprocess and _is_youtube(url):
            logger.info("Attempting YouTube subprocess fallback...")
            info = await self._extract_youtube_via_subprocess(url)
            return info, True
        return None, used_subprocess

    async def extract(self, url: str, for_list_formats: bool = False) -> Dict[str, Any]:
        """Извлекает метаданные видео через subprocess yt-dlp (async isolation)."""
        from app.core.process import run_subprocess
        
        opts = self._base_opts(for_list_formats=for_list_formats, url=url)
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-json",
            "--no-download",
        ]
        
        if opts.get("quiet"):
            cmd.append("--quiet")
        if opts.get("no_warnings"):
            cmd.append("--no-warnings")
        if opts.get("noplaylist"):
            cmd.append("--no-playlist")
        if opts.get("force_ipv4"):
            cmd.append("--force-ipv4")
        if opts.get("geo_bypass"):
            cmd.append("--geo-bypass")
        if opts.get("ignoreconfig"):
            cmd.append("--ignore-config")
        
        if opts.get("cookiefile"):
            cmd.extend(["--cookies", opts["cookiefile"]])
            
        if opts.get("proxy"):
            cmd.extend(["--proxy", opts["proxy"]])
            
        if opts.get("user_agent"):
            cmd.extend(["--user-agent", opts["user_agent"]])
            
        if opts.get("extractor_args"):
            for ext, args in opts["extractor_args"].items():
                for arg_key, arg_val in args.items():
                    val_str = ""
                    if isinstance(arg_val, list):
                        val_str = ",".join(str(v) for v in arg_val)
                    else:
                        val_str = str(arg_val)
                    cmd.extend(["--extractor-args", f"{ext}:{arg_key}={val_str}"])

        cmd.append(url)
        
        async with run_subprocess(cmd, timeout=120) as handle:
            stdout, stderr = await handle.proc.communicate()
            if handle.proc.returncode != 0:
                err_text = stderr.decode() if stderr else ""
                logger.error("yt-dlp extract failed: retcode=%s, stderr=%s", handle.proc.returncode, err_text)
                raise ExtractionError(f"yt-dlp execution failed: {err_text}")
            
            if not stdout:
                raise ExtractionError("yt-dlp returned empty stdout")
                
            return json.loads(stdout.decode())  # type: ignore

    async def list_formats(self, url: str, max_items: int = 12) -> ExtractionResult:
        """Извлекает форматы видео с обработкой ошибок."""
        info: Optional[Dict[str, Any]] = None
        used_subprocess = False

        # TikTok pre-routing: detect slideshows by URL pattern BEFORE extraction
        if _is_tiktok(url):
            content_type = classify_tiktok_content(url)
            if content_type == "slideshow":
                logger.info("TikTok /photo/ URL, routing to slideshow: %s", url)
                special_format = get_special_format(url)
                return ExtractionResult(
                    title="TikTok Slideshow",
                    formats=[],
                    special_format=special_format,
                    duration_str="—",
                    is_slideshow=True,
                    info_json_path=None,
                    thumbnail_url=None,
                )

        try:
            info = await self.extract(url, for_list_formats=True)
        except Exception as e:
            info, used_subprocess = await self._attempt_youtube_fallback(url, used_subprocess)

            if not info:
                error_msg = str(e).lower()

                if _is_tiktok(url):
                    error_class = classify_tiktok_error(error_msg)

                    if error_class == TikTokError.AUTH_REQUIRED:
                        logger.info(
                            "TikTok auth required, returning proxy fallback formats: %s",
                            url,
                        )
                        formats = []
                        if self.tiktok_proxy:
                            formats.append(
                                FormatItem(
                                    format_id="gallerydl_fallback",
                                    ext="mp4",
                                    height=None,
                                    filesize=None,
                                    is_tiktok=True,
                                    format_note="proxy_fallback",
                                )
                            )
                        formats.append(
                            FormatItem(
                                format_id="tikwm_fallback",
                                ext="mp4",
                                height=None,
                                filesize=None,
                                is_tiktok=True,
                                format_note="alt_fallback",
                            )
                        )
                        special_format = get_special_format(url)
                        return ExtractionResult(
                            title="TikTok Video",
                            formats=formats,
                            special_format=special_format,
                            duration_str="—",
                            is_slideshow=False,
                            info_json_path=None,
                            thumbnail_url=None,
                            tiktok_auth_error=True,
                        )

                    elif error_class == TikTokError.SLIDESHOW:
                        logger.info(
                            "TikTok unsupported URL, routing to slideshow: %s", url
                        )
                        special_format = get_special_format(url)
                        return ExtractionResult(
                            title="TikTok Slideshow",
                            formats=[],
                            special_format=special_format,
                            duration_str="—",
                            is_slideshow=True,
                            info_json_path=None,
                            thumbnail_url=None,
                        )

                    elif error_class == TikTokError.FORBIDDEN:
                        raise AccessDeniedError(Texts.SVC_ACCESS_DENIED)

                    elif error_class == TikTokError.NOT_FOUND:
                        raise VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)

                    elif error_class == TikTokError.LIVE:
                        raise LiveStreamError(Texts.SVC_LIVE_NOT_SUPPORTED)

                    else:  # TikTokError.GENERIC
                        logger.info(
                            "TikTok extraction failed (unknown error), "
                            "trying slideshow fallback: %s",
                            url,
                        )
                        special_format = get_special_format(url)
                        return ExtractionResult(
                            title="TikTok Slideshow",
                            formats=[],
                            special_format=special_format,
                            duration_str="—",
                            is_slideshow=True,
                            info_json_path=None,
                            thumbnail_url=None,
                        )

                # Non-TikTok error handling
                if "403" in error_msg or "forbidden" in error_msg:
                    raise AccessDeniedError(Texts.SVC_ACCESS_DENIED)
                elif "404" in error_msg or "not found" in error_msg:
                    raise VideoNotFoundError(Texts.SVC_VIDEO_NOT_FOUND)
                elif (
                    "log in" in error_msg
                    or "cookies" in error_msg
                    or "sign in" in error_msg
                ):
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
                    raise ExtractionError(
                        Texts.SVC_EXTRACTION_ERROR.format(detail=msg[:300])
                    )

        if info.get("is_live") or info.get("live_status") == "is_live":
            raise LiveStreamError(
                "⚠️ Это прямая трансляция. Загрузка активных стримов не поддерживается."
            )

        title = info.get("title") or Texts.SVC_DEFAULT_TITLE
        duration_sec = info.get("duration")
        duration_str = _format_duration(duration_sec)
        thumbnail_url: Optional[str] = info.get("thumbnail")

        duration_factor = None
        if duration_sec:
            try:
                duration_factor = float(duration_sec) * BITRATE_COEFFICIENT
            except (ValueError, TypeError):
                duration_factor = None

        raw_formats = info.get("formats", [])
        if not raw_formats:
            info2, used_subprocess = await self._attempt_youtube_fallback(
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
            info2, used_subprocess = await self._attempt_youtube_fallback(
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

        # Cache raw extraction info as JSON for download reuse (--load-info-json).
        # Saves 3–15s per download by skipping re-extraction in the download phase.
        info_json_path: Optional[str] = None
        if info:
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

        return ExtractionResult(
            title=title,
            formats=formats,
            special_format=special_format,
            duration_str=duration_str,
            is_slideshow=is_slideshow,
            info_json_path=info_json_path,
            thumbnail_url=thumbnail_url,
        )

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
