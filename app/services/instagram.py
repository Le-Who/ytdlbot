"""
Instagram Service — Stories & Highlights with Rich Selection UX

Wraps `instaloader` to fetch story/highlight metadata and download media.
All instaloader calls are wrapped in `asyncio.to_thread` since instaloader
is entirely synchronous.

Session Management:
  - Requires IG_SESSION_B64 (base64-encoded instaloader session file)
    and IG_SESSION_USER (Instagram username the session belongs to).
  - Session file is decoded at startup and written to a temp path.
  - If session expires, the service logs a clear error and returns gracefully.
"""

import asyncio
import base64
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import instaloader

from app.core.config import IG_SESSION_B64, IG_SESSION_USER, TEMP_DIR
from app.core.utils import safe_remove

__all__ = ["InstagramService", "IGStoryItem", "IGHighlight"]

logger = logging.getLogger("app.services.instagram")

# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class IGStoryItem:
    """A single story item (photo or video) with metadata."""

    mediaid: str
    is_video: bool
    url: str  # direct media URL
    thumbnail_url: str  # thumbnail for preview
    timestamp: datetime
    duration: Optional[float] = None  # seconds, for videos
    typename: str = ""  # "GraphStoryImage" or "GraphStoryVideo"

    @property
    def type_emoji(self) -> str:
        return "🎬" if self.is_video else "📸"

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M")

    @property
    def date_str(self) -> str:
        return self.timestamp.strftime("%d.%m")

    @property
    def duration_str(self) -> str:
        if self.duration:
            return f"({int(self.duration)}с)"
        return ""

    @property
    def label(self) -> str:
        """Human-readable label for inline button / caption."""
        parts = [self.type_emoji, self.date_str, self.time_str]
        if self.duration_str:
            parts.append(self.duration_str)
        return " ".join(parts)


@dataclass
class IGHighlight:
    """A highlight reel with metadata."""

    highlight_id: str
    title: str
    cover_url: str
    item_count: int

    @property
    def label(self) -> str:
        return f"📁 {self.title} ({self.item_count})"


@dataclass
class IGProfileMedia:
    """Aggregated stories + highlights for a user profile."""

    username: str
    stories: List[IGStoryItem] = field(default_factory=list)
    highlights: List[IGHighlight] = field(default_factory=list)
    error: Optional[str] = None


# ── URL Parsing ──────────────────────────────────────────────────────────────

_IG_PROFILE_RE = re.compile(
    r"instagram\.com/(?:@)?([A-Za-z0-9_.]+)/?(?:\?.*)?$"
)
_IG_STORIES_RE = re.compile(
    r"instagram\.com/stories/([A-Za-z0-9_.]+)(?:/(\d+))?/?(?:\?.*)?$"
)
_IG_POST_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)"
)


def parse_instagram_url(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Parse an Instagram URL into (type, username_or_shortcode, optional_item_id).

    Returns:
        ("stories", username, story_id_or_None)
        ("profile", username, None)
        ("post", shortcode, None)
        ("unknown", None, None)
    """
    m = _IG_STORIES_RE.search(url)
    if m:
        return "stories", m.group(1), m.group(2)

    m = _IG_POST_RE.search(url)
    if m:
        return "post", m.group(1), None

    m = _IG_PROFILE_RE.search(url)
    if m:
        username = m.group(1)
        # Filter out known IG paths that aren't profiles
        if username in (
            "stories", "p", "reel", "reels", "explore", "accounts",
            "direct", "tv", "about", "developer", "legal",
        ):
            return "unknown", None, None
        return "profile", username, None

    return "unknown", None, None


# ── Session Management ───────────────────────────────────────────────────────

_loader: Optional[instaloader.Instaloader] = None
_session_path: Optional[str] = None


def _get_loader() -> Optional[instaloader.Instaloader]:
    """Lazily initialize and return a configured Instaloader instance."""
    global _loader, _session_path

    if _loader is not None:
        return _loader

    if not IG_SESSION_B64 or not IG_SESSION_USER:
        logger.warning(
            "[INSTAGRAM] IG_SESSION_B64 or IG_SESSION_USER not set. "
            "Instagram stories/highlights will not work."
        )
        return None

    try:
        # Decode session file from base64
        session_bytes = base64.b64decode(IG_SESSION_B64)
        _session_path = os.path.join(
            tempfile.gettempdir(), f"ig_session_{uuid.uuid4().hex}"
        )
        with open(_session_path, "wb") as f:
            f.write(session_bytes)

        L = instaloader.Instaloader(
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        L.load_session_from_file(IG_SESSION_USER, _session_path)
        L.context.quiet = True

        logger.info("[INSTAGRAM] Session loaded for user: %s", IG_SESSION_USER)
        _loader = L
        return L

    except Exception as e:
        logger.error("[INSTAGRAM] Failed to load session: %s", e)
        if _session_path and os.path.exists(_session_path):
            os.unlink(_session_path)
        return None


# ── Core Service ─────────────────────────────────────────────────────────────


class InstagramService:
    """Fetches Instagram stories, highlights, and posts via Instaloader."""

    @staticmethod
    async def get_profile_media(username: str) -> IGProfileMedia:
        """Fetch stories and highlights metadata for a user.
        Returns IGProfileMedia with lists of stories and highlights.
        All instaloader calls run in a thread to avoid blocking asyncio.
        """
        result = IGProfileMedia(username=username)

        def _fetch() -> IGProfileMedia:
            L = _get_loader()
            if not L:
                result.error = (
                    "⚠️ Instagram сессия не настроена. "
                    "Установите IG_SESSION_B64 и IG_SESSION_USER."
                )
                return result

            try:
                profile = instaloader.Profile.from_username(
                    L.context, username
                )
            except instaloader.exceptions.ProfileNotExistsException:
                result.error = f"⚠️ Профиль @{username} не найден."
                return result
            except instaloader.exceptions.LoginRequiredException:
                result.error = (
                    "⚠️ Сессия Instagram истекла. "
                    "Обновите IG_SESSION_B64."
                )
                return result
            except Exception as e:
                logger.error("[INSTAGRAM] Profile fetch error: %s", e)
                result.error = f"⚠️ Ошибка загрузки профиля: {str(e)[:100]}"
                return result

            # Fetch stories
            try:
                for story in L.get_stories(userids=[profile.userid]):
                    for item in story.get_items():
                        ig_item = IGStoryItem(
                            mediaid=str(item.mediaid),
                            is_video=item.is_video,
                            url=item.video_url if item.is_video else item.url,
                            thumbnail_url=item.url,  # always image URL for thumb
                            timestamp=item.date_utc,
                            duration=getattr(item, "video_duration", None),
                            typename=item.typename or "",
                        )
                        result.stories.append(ig_item)
            except instaloader.exceptions.LoginRequiredException:
                result.error = "⚠️ Сессия Instagram истекла для историй."
                return result
            except Exception as e:
                logger.warning("[INSTAGRAM] Stories fetch error: %s", e)
                # Continue to highlights even if stories fail

            # Fetch highlights
            try:
                for highlight in L.get_highlights(profile):
                    items_count = 0
                    cover = ""
                    try:
                        # Count items and get cover
                        cover = highlight.cover_url or ""
                        for _ in highlight.get_items():
                            items_count += 1
                    except Exception:
                        pass

                    result.highlights.append(
                        IGHighlight(
                            highlight_id=str(highlight.unique_id),
                            title=highlight.title or "Highlight",
                            cover_url=cover,
                            item_count=items_count,
                        )
                    )
            except instaloader.exceptions.LoginRequiredException:
                if not result.error:
                    result.error = "⚠️ Сессия Instagram истекла для хайлайтов."
            except Exception as e:
                logger.warning("[INSTAGRAM] Highlights fetch error: %s", e)

            return result

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def get_highlight_items(
        highlight_id: str,
    ) -> Tuple[List[IGStoryItem], Optional[str]]:
        """Fetch all items in a specific highlight.
        Returns (items, error_message).
        """

        def _fetch() -> Tuple[List[IGStoryItem], Optional[str]]:
            L = _get_loader()
            if not L:
                return [], "⚠️ Instagram сессия не настроена."

            try:
                # Instaloader's highlight API
                node = {"id": highlight_id}
                highlight = instaloader.Highlight(L.context, node)  # type: ignore[arg-type]
                items: List[IGStoryItem] = []
                for item in highlight.get_items():
                    items.append(
                        IGStoryItem(
                            mediaid=str(item.mediaid),
                            is_video=item.is_video,
                            url=(item.video_url or item.url) if item.is_video else item.url,
                            thumbnail_url=item.url,
                            timestamp=item.date_utc,
                            duration=getattr(item, "video_duration", None),
                            typename=item.typename or "",
                        )
                    )
                return items, None
            except Exception as e:
                logger.error("[INSTAGRAM] Highlight fetch error: %s", e)
                return [], f"⚠️ Ошибка загрузки хайлайта: {str(e)[:100]}"

        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def download_story_item(
        item: IGStoryItem,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Download a single story/highlight item to disk.
        Returns (file_path, error_message).
        """
        from curl_cffi.requests import AsyncSession as CurlSession

        ext = "mp4" if item.is_video else "jpg"
        out_path = os.path.join(TEMP_DIR, f"ig_{uuid.uuid4().hex}.{ext}")

        try:
            async with CurlSession() as session:
                resp = await session.get(
                    item.url, impersonate="chrome", timeout=60, stream=True
                )
                if resp.status_code >= 400:
                    return None, f"HTTP {resp.status_code}"

                total = 0

                def _write(path: str, data: bytes) -> None:
                    mode = "ab" if os.path.exists(path) else "wb"
                    with open(path, mode) as f:
                        f.write(data)

                async for chunk in resp.aiter_content():
                    if chunk:
                        await asyncio.to_thread(_write, out_path, chunk)
                        total += len(chunk)

            if total == 0:
                await asyncio.to_thread(safe_remove, out_path)
                return None, "Empty response"

            logger.info(
                "[INSTAGRAM] Downloaded %s (%.1f MB)",
                out_path,
                total / (1024 * 1024),
            )
            return out_path, None

        except Exception as e:
            logger.error("[INSTAGRAM] Download error: %s", e)
            if os.path.exists(out_path):
                await asyncio.to_thread(safe_remove, out_path)
            return None, str(e)

    @staticmethod
    async def download_post(url: str) -> Tuple[Optional[str], Optional[str]]:
        """Download an Instagram post/reel via yt-dlp fallback.
        Returns (file_path, error_message).
        """
        # For posts/reels, delegate to yt-dlp which handles public IG posts
        from app.services.cobalt import CobaltService

        logger.info("[INSTAGRAM] Attempting post download via Cobalt: %s", url)
        c_res = await CobaltService.process(url)

        if c_res.status in ("tunnel", "redirect") and c_res.url:
            from curl_cffi.requests import AsyncSession as CurlSession

            out_path = os.path.join(TEMP_DIR, f"ig_post_{uuid.uuid4().hex}.mp4")
            try:
                async with CurlSession() as session:
                    resp = await session.get(
                        c_res.url, impersonate="chrome", timeout=60, stream=True
                    )
                    if resp.status_code >= 400:
                        return None, f"HTTP {resp.status_code}"

                    total = 0

                    def _write(path: str, data: bytes) -> None:
                        mode = "ab" if os.path.exists(path) else "wb"
                        with open(path, mode) as f:
                            f.write(data)

                    async for chunk in resp.aiter_content():
                        if chunk:
                            await asyncio.to_thread(_write, out_path, chunk)
                            total += len(chunk)

                if total == 0:
                    await asyncio.to_thread(safe_remove, out_path)
                    return None, "Empty response"

                return out_path, None
            except Exception as e:
                if os.path.exists(out_path):
                    await asyncio.to_thread(safe_remove, out_path)
                return None, str(e)

        return None, c_res.error_message or "Cobalt не смог обработать пост"


def is_instagram_url(url: str) -> bool:
    """Check if a URL is an Instagram URL."""
    return "instagram.com" in url or "instagr.am" in url
