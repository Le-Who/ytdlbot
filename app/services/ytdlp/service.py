import shutil
import json
import os
import sys
import logging
from typing import Dict, Any, List, Optional

__all__ = ["YtDlpService"]

from app.core.config import TIKTOK_PROXY, TEMP_DIR
from .models import FormatItem, FormatMetadata, ExtractionResult
from .cookies import PlatformCookiesManager
from .builders import YtDlpCLIBuilder
from .parsers import (
    parse_format_metadata,
    create_format_item,
    deduplicate_formats,
    _format_duration,
    get_special_format,
    _is_tiktok,
    _is_youtube,
    _is_pinterest,
    detect_tiktok_slideshow,
    classify_tiktok_content,
    BITRATE_COEFFICIENT,
)
from .exceptions import (
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
    map_ytdlp_error,
)
from app.core.texts import Texts

logger = logging.getLogger("ytdlp_service")


class YtDlpService:
    """Сервис для работы с yt-dlp (Facade)"""

    def __init__(self):
        self.cookies_manager = PlatformCookiesManager()
        self.tiktok_proxy = TIKTOK_PROXY
        self.has_aria2 = bool(shutil.which("aria2c"))

        # Initialize builder without hardcoded youtube clients
        self.builder = YtDlpCLIBuilder()

        if self.has_aria2:
            logger.info("✅ aria2c found — multi-connection downloads enabled")
        if self.tiktok_proxy:
            logger.info("🔒 TikTok proxy configured: %s", self.tiktok_proxy)

    @property
    def cookies_path(self) -> Optional[str]:
        """Backward-compat: return TikTok cookies (used by slideshow etc.)"""
        return self.cookies_manager.tiktok_cookies_path

    async def extract(
        self, url: str, for_list_formats: bool = False, fallback_clients: bool = False
    ) -> Dict[str, Any]:
        """Извлекает метаданные видео через subprocess yt-dlp (async isolation)."""
        from app.core.process import run_subprocess

        cookies = self.cookies_manager.get_cookies_path(url)
        proxy = self.tiktok_proxy if _is_tiktok(url) else None

        cmd = self.builder.build_extraction_cmd(
            url=url,
            cookies_path=cookies,
            proxy=proxy,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            fallback_clients=fallback_clients,
        )
        # Ensure we use the current python executable for stability
        cmd[0] = sys.executable
        cmd.insert(1, "-m")
        cmd.insert(2, "yt_dlp")

        async with run_subprocess(cmd, timeout=120) as handle:
            assert handle.proc.stdout is not None
            stdout = await handle.proc.stdout.read()
            await handle.wait()

            if handle.proc.returncode != 0:
                err_text = (
                    b"".join(handle.stderr_data).decode(errors="ignore")
                    if handle.stderr_data
                    else ""
                )
                logger.error(
                    "yt-dlp extract failed: retcode=%s, stderr=%s",
                    handle.proc.returncode,
                    err_text,
                )
                # Map error to specific exception type using the new strict mapper mapping
                raise map_ytdlp_error(err_text, url)

            if not stdout:
                raise ExtractionError("yt-dlp returned empty stdout")

            return json.loads(stdout.decode())  # type: ignore

    async def list_formats(self, url: str, max_items: int = 12) -> ExtractionResult:
        """Извлекает форматы видео с обработкой ошибок."""
        info: Optional[Dict[str, Any]] = None

        # TikTok pre-routing: detect slideshows by URL pattern BEFORE extraction
        if _is_tiktok(url):
            logger.info("TikTok URL detected, bypassing yt-dlp: %s", url)
            content_type = classify_tiktok_content(url)
            is_slideshow = content_type == "slideshow"
            title = "TikTok Slideshow" if is_slideshow else "TikTok Video"
            formats = []
            if not is_slideshow:
                formats.append(
                    FormatItem(
                        format_id="tikwm_fallback",
                        ext="mp4",
                        height=None,
                        filesize=None,
                        is_tiktok=True,
                        format_note="optimal",
                    )
                )
            return ExtractionResult(
                title=title,
                formats=formats,
                special_format=get_special_format(url),
                duration_str="—",
                is_slideshow=is_slideshow,
                info_json_path=None,
                thumbnail_url=None,
                youtube_fallback=False,
                tiktok_auth_error=False,
            )

        if _is_pinterest(url):
            logger.info("Pinterest URL detected, bypassing yt-dlp: %s", url)
            formats = [
                FormatItem(
                    format_id="pinterest_native",
                    ext="mp4",
                    height=None,
                    filesize=None,
                    is_tiktok=False,
                    format_note="native",
                )
            ]
            return ExtractionResult(
                title="Pinterest Media",
                formats=formats,
                special_format=get_special_format(url),
                duration_str="—",
                is_slideshow=False,
                info_json_path=None,
                thumbnail_url=None,
                youtube_fallback=False,
            )

        youtube_fallback = False

        try:
            info = await self.extract(url, for_list_formats=True)
        except AccessDeniedError as e:
            if _is_youtube(url):
                logger.warning(
                    "YouTube extraction failed with 403. Retrying with ios,android fallback clients: %s",
                    url,
                )
                info = await self.extract(
                    url, for_list_formats=True, fallback_clients=True
                )
                youtube_fallback = True
            else:
                raise e
        except VideoNotFoundError:
            raise
        except Exception as e:
            # Re-raise known API exceptions that should trigger orchestration fallback
            logger.error("YtDlp Extraction Error: %s", e, exc_info=True)
            raise ExtractionError(
                Texts.SVC_EXTRACTION_ERROR.format(detail=str(e)[:300])
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
        is_tiktok_url = _is_tiktok(url)
        formats_meta: List[FormatMetadata] = []
        for raw_fmt in raw_formats:
            fmt = parse_format_metadata(raw_fmt, duration_factor, is_tiktok_url)
            if fmt:
                formats_meta.append(fmt)

        formats_meta.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
        formats_meta = deduplicate_formats(formats_meta, is_tiktok_url)
        formats_meta = formats_meta[:max_items]

        formats = [create_format_item(f, is_tiktok_url) for f in formats_meta]
        is_slideshow = detect_tiktok_slideshow(info, url)

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
            special_format=get_special_format(url),
            duration_str=duration_str,
            is_slideshow=is_slideshow,
            info_json_path=info_json_path,
            thumbnail_url=thumbnail_url,
            youtube_fallback=youtube_fallback,
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
        fallback_clients: bool = False,
        pipe_mode: bool = False,
        section: Optional[str] = None,
    ) -> List[str]:
        """Proxy to new CLI builder"""
        cookies = self.cookies_manager.get_cookies_path(page_url)
        is_tiktok = _is_tiktok(page_url)

        cmd = self.builder.build_download_cmd(
            url=page_url,
            format_id=format_id,
            output_path=output,
            height=height,
            cookies_path=cookies,
            proxy=self.tiktok_proxy if is_tiktok else None,
            max_filesize_mb=max_filesize,
            use_aria2=use_aria2 and self.has_aria2,
            info_json_path=info_json_path,
            fallback_clients=fallback_clients,
            pipe_mode=pipe_mode,
            section=section,
        )

        # Always use exact python executable to avoid environment path issues
        cmd[0] = sys.executable
        cmd.insert(1, "-m")
        cmd.insert(2, "yt_dlp")
        return cmd
