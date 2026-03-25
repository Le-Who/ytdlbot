"""
Instagram Service — Stories & Highlights with Rich Selection UX

Implemented via Tier 1 Anonymous Mobile API Forgery (GraphQL Bypass).
Uses `curl_cffi` to bypass datacenter TLS fingerprints.
"""

import asyncio
import logging
import os
import re
import uuid
import base64
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from curl_cffi.requests import AsyncSession
from app.core.config import TEMP_DIR, IG_SESSION_B64, IG_PROXY
from app.core.utils import safe_remove

__all__ = ["InstagramService", "IGStoryItem", "IGHighlight", "parse_instagram_url"]

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
    duration: Optional[float] = None
    typename: str = ""

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
    r"(?:instagram\.com|instagr\.am)/(?:@)?([A-Za-z0-9_.]+)/?(?:\?.*)?$"
)
_IG_STORIES_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/stories/([A-Za-z0-9_.]+)(?:/(\d+))?/?(?:\?.*)?$"
)
_IG_HIGHLIGHT_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/stories/highlights/(\d+)"
)
_IG_POST_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:p|reel|reels)/([A-Za-z0-9_-]+)"
)


def parse_instagram_url(url: str) -> Tuple[str, Optional[str], Optional[str]]:
    m = _IG_HIGHLIGHT_RE.search(url)
    if m:
        return "highlight", m.group(1), None

    m = _IG_STORIES_RE.search(url)
    if m:
        return "stories", m.group(1), m.group(2)

    m = _IG_POST_RE.search(url)
    if m:
        return "post", m.group(1), None

    m = _IG_PROFILE_RE.search(url)
    if m:
        username = m.group(1)
        if username in (
            "stories",
            "p",
            "reel",
            "reels",
            "explore",
            "accounts",
            "direct",
            "tv",
            "about",
            "developer",
            "legal",
        ):
            return "unknown", None, None
        return "profile", username, None

    return "unknown", None, None


def is_instagram_url(url: str) -> bool:
    return bool(parse_instagram_url(url)[0] != "unknown")


# ── Core Service ─────────────────────────────────────────────────────────────


class InstagramService:
    """Fetches Instagram media using anonymous mobile endpoint forgery via curl_cffi."""

    IG_APP_ID = "936619743392459"
    IMPERSONATE = "chrome110"

    _ig_cookies: Optional[Dict[str, str]] = None
    _session_initialized: bool = False

    @classmethod
    def _init_session(cls) -> None:
        """Parses the legacy instaloader session to extract the sessionid cookie."""
        if cls._session_initialized:
            return

        cls._session_initialized = True
        if not IG_SESSION_B64:
            return

        try:
            data = base64.b64decode(IG_SESSION_B64)
            cookies = pickle.loads(data)

            # Instaloader saves either as RequestsCookieJar or dict
            result_cookies = {}
            if isinstance(cookies, dict):
                for k, v in cookies.items():
                    if isinstance(v, str) and v:
                        result_cookies[k] = v
            else:
                for cookie in cookies:
                    if (
                        hasattr(cookie, "name")
                        and hasattr(cookie, "value")
                        and cookie.value
                    ):
                        result_cookies[cookie.name] = cookie.value

            if "sessionid" in result_cookies:
                cls._ig_cookies = result_cookies
                logger.info(
                    "[INSTAGRAM] Successfully restored full cookie suite from IG_SESSION_B64."
                )
            else:
                logger.warning(
                    "[INSTAGRAM] IG_SESSION_B64 parsed successfully, but no sessionid found (session expired?)."
                )

        except Exception as e:
            logger.error(
                "[INSTAGRAM] Failed to parse IG_SESSION_B64: %s", type(e).__name__
            )

    @classmethod
    async def get_profile_media(cls, username: str) -> IGProfileMedia:
        """Fetch basic profile + highlights + stories anonymously."""
        cls._init_session()
        result = IGProfileMedia(username=username)

        try:
            proxy_dict = {"all": IG_PROXY} if IG_PROXY else None
            # 1. Fetch profile ANONYMOUSLY to avoid session flagging and HTML challenge pages
            async with AsyncSession(impersonate=cls.IMPERSONATE, proxies=proxy_dict) as session:  # type: ignore
                doc = await session.get(
                    f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                    headers={"X-IG-App-ID": cls.IG_APP_ID, "X-Requested-With": "XMLHttpRequest"}
                )
                if doc.status_code == 404:
                    result.error = f"⚠️ Профиль @{username} не найден."
                    return result
                
                try:
                    data = doc.json()
                    user_data = data["data"]["user"]
                    uid = user_data["id"]
                except Exception:
                    logger.warning("[INSTAGRAM] Failed to parse web_profile_info for %s: %s", username, doc.status_code)
                    result.error = f"⚠️ Профиль @{username} недоступен (IP Block)."
                    return result

                # Parse highlights if available
                if user_data.get("highlight_reel_count", 0) > 0:
                    highlights_edges = user_data.get("edge_highlight_reels", {}).get("edges", [])
                    for edge in highlights_edges:
                        node = edge["node"]
                        result.highlights.append(
                            IGHighlight(
                                highlight_id=node["id"],
                                title=node["title"],
                                cover_url=node["cover_media_cropped_thumbnail"]["url"],
                                item_count=1 # Approximate since edge_highlight_reels doesn't always give item count cleanly
                            )
                        )

                # Fetch stories if authenticated session is available
                if cls._ig_cookies:
                    async with AsyncSession(impersonate=cls.IMPERSONATE, cookies=cls._ig_cookies, proxies=proxy_dict) as auth_session:  # type: ignore
                        stories = await cls._fetch_reels_media(auth_session, [uid])
                        result.stories = stories.get(uid, [])
                else:
                    logger.warning("[INSTAGRAM] No authenticated session for stories for %s", username)


                return result

        except Exception as e:
            logger.error("[INSTAGRAM] Profile fetch error: %s", e)
            result.error = f"⚠️ Ошибка загрузки профиля: {str(e)[:100]}"
            return result

    @classmethod
    async def get_highlight_items(
        cls, highlight_id: str
    ) -> Tuple[List[IGStoryItem], Optional[str]]:
        """Fetch items in a specific highlight securely, authenticating if possible."""
        cls._init_session()
        try:
            proxy_dict = {"all": IG_PROXY} if IG_PROXY else None
            async with AsyncSession(
                impersonate=cls.IMPERSONATE, cookies=cls._ig_cookies, proxies=proxy_dict
            ) as session:  # type: ignore
                hid = f"highlight:{highlight_id}"
                items = await cls._fetch_reels_media(session, [hid])
                return items.get(hid, []), None
        except Exception as e:
            logger.error("[INSTAGRAM] Highlight items error: %s", e)
            return [], f"⚠️ Ошибка загрузки хайлайта: {str(e)[:100]}"

    @classmethod
    async def _fetch_reels_media(
        cls, session: AsyncSession, ids: List[str]
    ) -> Dict[str, List[IGStoryItem]]:
        """Hits the iPhone reels_media endpoint using curl_cffi."""
        try:
            ids_param = "&".join(f"reel_ids={x}" for x in ids)
            resp = await session.get(
                f"https://i.instagram.com/api/v1/feed/reels_media/?{ids_param}",
                headers={
                    "X-IG-App-ID": cls.IG_APP_ID,
                    "User-Agent": "Instagram 219.0.0.12.117 Android",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "[INSTAGRAM] reels_media failed with %s", resp.status_code
                )
                return {}

            reels = resp.json().get("reels", {})
            out = {}
            for reel_id, reel_data in reels.items():
                items_data = reel_data.get("items", [])
                parsed: List[IGStoryItem] = []
                for item in items_data:
                    is_video = item.get("media_type") == 2
                    duration = float(item.get("video_duration", 0.0))
                    timestamp_val = item.get("taken_at", 0)
                    dt = datetime.fromtimestamp(timestamp_val, tz=timezone.utc)

                    url = ""
                    thumb = ""
                    if is_video and "video_versions" in item and item["video_versions"]:
                        url = item["video_versions"][0]["url"]
                    elif (
                        "image_versions2" in item
                        and "candidates" in item["image_versions2"]
                        and item["image_versions2"]["candidates"]
                    ):
                        url = item["image_versions2"]["candidates"][0]["url"]
                    else:
                        continue

                    if (
                        "image_versions2" in item
                        and "candidates" in item["image_versions2"]
                        and item["image_versions2"]["candidates"]
                    ):
                        thumb = item["image_versions2"]["candidates"][0]["url"]
                    else:
                        thumb = url

                    parsed.append(
                        IGStoryItem(
                            mediaid=str(item.get("pk", item.get("id"))),
                            is_video=is_video,
                            url=url,
                            thumbnail_url=thumb,
                            timestamp=dt,
                            duration=duration if duration > 0 else None,
                            typename="GraphStoryVideo"
                            if is_video
                            else "GraphStoryImage",
                        )
                    )
                out[reel_id] = parsed
            return out
        except Exception as e:
            logger.error("[INSTAGRAM] _fetch_reels_media error: %s", e)
            return {}

    @classmethod
    async def download_story_item(
        cls, item: IGStoryItem
    ) -> Tuple[Optional[str], Optional[str]]:
        """Download high-res video/image from IG URL -> local temp file via chunked stream."""
        from urllib.parse import urlparse

        if not item.url:
            return None, "⚠️ Пустой URL медиа."

        parsed = urlparse(item.url)
        ext = ".mp4" if item.is_video else ".jpg"
        if parsed.path:
            possible_ext = os.path.splitext(parsed.path)[1]
            if possible_ext in (".mp4", ".jpg", ".jpeg", ".webp"):
                ext = possible_ext

        out_path = os.path.join(TEMP_DIR, f"ig_{uuid.uuid4().hex}{ext}")

        try:
            cls._init_session()
            proxy_dict = {"all": IG_PROXY} if IG_PROXY else None
            async with AsyncSession(
                impersonate=cls.IMPERSONATE, cookies=cls._ig_cookies, proxies=proxy_dict
            ) as session:  # type: ignore
                resp = await session.get(item.url, stream=True)
                if resp.status_code != 200:
                    return None, f"⚠️ Ошибка CDN Instagram: {resp.status_code}"

                def _write():
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content():
                            f.write(chunk)

                await asyncio.to_thread(_write)
                return out_path, None

        except Exception as e:
            logger.error("[INSTAGRAM] Download error: %s", e)
            safe_remove(out_path)
            return None, "⚠️ Внутренняя ошибка загрузки."

    @staticmethod
    async def download_post(url: str) -> Tuple[Optional[str], Optional[str]]:
        """Fallback to Cobalt for direct post formats."""
        try:
            from app.services.cobalt import CobaltService

            c_res = await CobaltService.process(url)
            if not c_res:
                return None, "⚠️ Cobalt не вернул данные."

            if getattr(c_res, "status", None) == "picker":
                return (
                    None,
                    "⚠️ Multi-photo карусели скачивайте через основное меню (пока не поддерживаются в этом обработчике).",
                )

            if not c_res.url:
                return None, "⚠️ Cobalt вернул пустой URL."

            out_path = os.path.join(TEMP_DIR, f"ig_fallback_{uuid.uuid4().hex}.mp4")

            async with AsyncSession(impersonate="chrome110") as session:
                resp = await session.get(c_res.url, stream=True)
                if resp.status_code != 200:
                    return None, f"Cobalt CDN error: {resp.status_code}"

                def _write():
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content():
                            f.write(chunk)

                await asyncio.to_thread(_write)
                return out_path, None

        except Exception as e:
            from app.core.utils import safe_remove

            logger.error("[INSTAGRAM] Post download fallback error: %s", e)
            if "out_path" in locals():
                safe_remove(out_path)
            return None, "⚠️ Ошибка резервного канала (Cobalt)."
