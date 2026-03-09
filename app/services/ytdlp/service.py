import shutil
import json
import os
import sys
import logging
from typing import Dict, Any, List, Optional, Tuple

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

        # Determine player clients
        youtube_clients = ["ios", "android", "web"]
        self.builder = YtDlpCLIBuilder(youtube_player_clients=youtube_clients)

        if self.has_aria2:
            logger.info("✅ aria2c found — multi-connection downloads enabled")
        if self.tiktok_proxy:
            logger.info("🔒 TikTok proxy configured: %s", self.tiktok_proxy)

    @property
    def cookies_path(self) -> Optional[str]:
        """Backward-compat: return TikTok cookies (used by slideshow etc.)"""
        return self.cookies_manager.tiktok_cookies_path

    async def _extract_youtube_via_subprocess(
        self, url: str
    ) -> Optional[Dict[str, Any]]:
        """Fallback extraction for YouTube when standard fails"""
        # Kept for backward compat; but new YtDlpCLIBuilder already injects player clients.
        # We can just return None here for now because the base extract does it via the builder.
        # But to be safe, we will leave the structure.
        return None

    async def _attempt_youtube_fallback(
        self, url: str, used_subprocess: bool
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Attempts to fetch info via fallback"""
        # Since we use the unified builder, this fallback is essentially a no-op
        # because the original request already tried all clients.
        return None, used_subprocess

    async def extract(self, url: str, for_list_formats: bool = False) -> Dict[str, Any]:
        """Извлекает метаданные видео через subprocess yt-dlp (async isolation)."""
        from app.core.process import run_subprocess

        cookies = self.cookies_manager.get_cookies_path(url)
        proxy = self.tiktok_proxy if _is_tiktok(url) else None

        cmd = self.builder.build_extraction_cmd(
            url=url,
            cookies_path=cookies,
            proxy=proxy,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
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
            content_type = classify_tiktok_content(url)
            if content_type == "slideshow":
                logger.info("TikTok /photo/ URL, routing to slideshow: %s", url)
                return ExtractionResult(
                    title="TikTok Slideshow",
                    formats=[],
                    special_format=get_special_format(url),
                    duration_str="—",
                    is_slideshow=True,
                    info_json_path=None,
                    thumbnail_url=None,
                )

        try:
            info = await self.extract(url, for_list_formats=True)
        except Exception as e:
            # Re-raise known API exceptions that should trigger orchestration fallback
            if isinstance(e, (AccessDeniedError, VideoNotFoundError, LiveStreamError)):
                raise e

            error_msg = str(e).lower()
            if _is_tiktok(url):
                error_class = classify_tiktok_error(error_msg)

                # TEMPORARY: Retain TikTok proxy routing inside Service until Orchestrator is built
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
                    return ExtractionResult(
                        title="TikTok Video",
                        formats=formats,
                        special_format=get_special_format(url),
                        duration_str="—",
                        is_slideshow=False,
                        info_json_path=None,
                        thumbnail_url=None,
                        tiktok_auth_error=True,
                    )
                elif (
                    error_class == TikTokError.SLIDESHOW
                    or error_class == TikTokError.GENERIC
                ):
                    return ExtractionResult(
                        title="TikTok Slideshow",
                        formats=[],
                        special_format=get_special_format(url),
                        duration_str="—",
                        is_slideshow=True,
                        info_json_path=None,
                        thumbnail_url=None,
                    )

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
        )

        # Always use exact python executable to avoid environment path issues
        cmd[0] = sys.executable
        cmd.insert(1, "-m")
        cmd.insert(2, "yt_dlp")
        return cmd
