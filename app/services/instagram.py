"""
Instagram Service — Stories & Highlights with Rich Selection UX

Implemented via Tier 1 Anonymous Mobile API Forgery (GraphQL Bypass).
Uses `curl_cffi` to bypass datacenter TLS fingerprints.
"""

import logging
import os
import re
import uuid
import base64
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Literal

from curl_cffi.requests import AsyncSession
from app.core.config import TEMP_DIR, IG_SESSIONS_B64
from app.core.utils import safe_remove
from app.core import state

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


# ── Shortcode ↔ Media PK Conversion ─────────────────────────────────────────

_IG_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _extract_shortcode(url: str) -> Optional[str]:
    """Extract the shortcode from an Instagram /p/, /reel/, or /reels/ URL."""
    m = _IG_POST_RE.search(url)
    return m.group(1) if m else None


def _shortcode_to_media_pk(shortcode: str) -> str:
    """Convert an Instagram shortcode to numeric media PK (deterministic base64)."""
    media_pk = 0
    for char in shortcode:
        media_pk = media_pk * 64 + _IG_SHORTCODE_ALPHABET.index(char)
    return str(media_pk)


# ── Core Service ─────────────────────────────────────────────────────────────


class InstagramService:
    """Fetches Instagram media using anonymous mobile endpoint forgery via curl_cffi."""

    IG_APP_ID = "936619743392459"
    IMPERSONATE: Literal["chrome110"] = "chrome110"
    IG_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"

    _ig_cookie_pool: List[Dict[str, str]] = []
    _pool_initialized: bool = False
    _current_pool_index: int = 0

    @classmethod
    def _init_pool(cls) -> None:
        """Parses the comma-separated IG_SESSIONS_B64 environment into a robust rotation pool."""
        if cls._pool_initialized:
            return

        cls._pool_initialized = True
        cls._ig_cookie_pool = []

        if not IG_SESSIONS_B64:
            return

        for idx, session_b64 in enumerate(IG_SESSIONS_B64):
            try:
                data = base64.b64decode(session_b64)
                cookies = pickle.loads(data)

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
                    cls._ig_cookie_pool.append(result_cookies)
                else:
                    logger.warning(
                        "[INSTAGRAM] Session index %d parsed but no sessionid found (expired?).",
                        idx,
                    )

            except Exception as e:
                logger.error(
                    "[INSTAGRAM] Failed to parse IG_SESSIONS_B64 index %d: %s",
                    idx,
                    type(e).__name__,
                )

        if cls._ig_cookie_pool:
            logger.info(
                "[INSTAGRAM] Successfully loaded %d sessions into rotation pool.",
                len(cls._ig_cookie_pool),
            )

    @classmethod
    async def _get_available_session(
        cls,
    ) -> Tuple[Optional[int], Optional[Dict[str, str]]]:
        """Round-robin iterates over the session pool and claims the first allowed token from Redis limiter."""
        cls._init_pool()
        pool_size = len(cls._ig_cookie_pool)
        if pool_size == 0:
            return -1, None

        for _ in range(pool_size):
            idx = cls._current_pool_index
            cls._current_pool_index = (cls._current_pool_index + 1) % pool_size

            # Atomic global limit check via RedisTokenBucketLimiter
            if await state.limiter.allow_ig_fallback(idx):
                return idx, cls._ig_cookie_pool[idx]

        return -2, None

    @classmethod
    async def get_profile_media(cls, username: str) -> IGProfileMedia:
        """Fetch basic profile + highlights + stories anonymously."""
        cls._init_pool()
        result = IGProfileMedia(username=username)

        try:
            uid = None

            # 1. Fetch profile ANONYMOUSLY to avoid session flagging and HTML challenge pages
            async with AsyncSession(impersonate=cls.IMPERSONATE) as session:
                doc = await session.get(
                    f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                    headers={
                        "X-IG-App-ID": cls.IG_APP_ID,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                if doc.status_code == 404:
                    result.error = f"⚠️ Профиль @{username} не найден."
                    return result

                if doc.status_code == 200:
                    try:
                        data = doc.json()
                        user_data = data.get("data", {}).get("user", {})
                        if user_data:
                            uid = str(user_data["id"])
                            # Parse highlights if available
                            if user_data.get("highlight_reel_count", 0) > 0:
                                highlights_edges = user_data.get(
                                    "edge_highlight_reels", {}
                                ).get("edges", [])
                                for edge in highlights_edges:
                                    node = edge["node"]
                                    result.highlights.append(
                                        IGHighlight(
                                            highlight_id=node["id"],
                                            title=node["title"],
                                            cover_url=node[
                                                "cover_media_cropped_thumbnail"
                                            ]["url"],
                                            item_count=1,  # Approximate
                                        )
                                    )
                    except Exception as e:
                        logger.warning(
                            "[INSTAGRAM] Failed to parse web_profile_info for %s: %s",
                            username,
                            type(e).__name__,
                        )

            # 2. Fallback to Authenticated Mobile API if Anonymous Web API was blocked (401/302/JSON parse error)
            if not uid and cls._ig_cookie_pool:
                logger.info(
                    "[INSTAGRAM] Anonymous Web API blocked/failed (status %s). Falling back to Mobile API for %s",
                    doc.status_code,
                    username,
                )

                sess_idx, cookies_to_use = await cls._get_available_session()
                if not cookies_to_use:
                    result.error = "⏳ Инстаграм на паузе. Лимит профилей исчерпан для предотвращения бана."
                    return result

                async with AsyncSession(
                    impersonate=cls.IMPERSONATE, cookies=cookies_to_use
                ) as auth_session:
                    # Mobile fetch 1: UID
                    mobile_doc = await auth_session.get(
                        f"https://i.instagram.com/api/v1/users/{username}/usernameinfo/",
                        headers={"User-Agent": cls.IG_USER_AGENT},
                    )
                    if mobile_doc.status_code == 200:
                        try:
                            m_data = mobile_doc.json()
                            if "user" in m_data and "pk" in m_data["user"]:
                                uid = str(m_data["user"]["pk"])
                        except Exception as e:
                            logger.warning(
                                "[INSTAGRAM] Failed to parse usernameinfo for %s: %s",
                                username,
                                e,
                            )

                    # Mobile fetch 2: Highlights Tray
                    if uid:
                        tray_doc = await auth_session.get(
                            f"https://i.instagram.com/api/v1/highlights/{uid}/highlights_tray/",
                            headers={"User-Agent": cls.IG_USER_AGENT},
                        )
                        if tray_doc.status_code == 200:
                            try:
                                tray_data = tray_doc.json()
                                for edge in tray_data.get("tray", []):
                                    raw_id = str(edge.get("id", ""))
                                    hid = (
                                        raw_id.replace("highlight:", "")
                                        if "highlight:" in raw_id
                                        else raw_id
                                    )

                                    title = edge.get("title", "")
                                    cover_url = ""

                                    cover_media = edge.get("cover_media", {})
                                    if isinstance(cover_media, dict):
                                        cropped = cover_media.get(
                                            "cropped_image_version", {}
                                        )
                                        if isinstance(cropped, dict):
                                            cover_url = cropped.get("url", "")

                                    if not cover_url:
                                        # Default empty fallback to prevent pydantic/dataclass constraint errors
                                        cover_url = "https://scontent.cdninstagram.com/v/t51.2885-15/e35/c0.0.1080.1080a/s150x150/1_1_2.jpg"

                                    if hid:
                                        result.highlights.append(
                                            IGHighlight(
                                                highlight_id=hid,
                                                title=title,
                                                cover_url=cover_url,
                                                item_count=edge.get("media_count", 1),
                                            )
                                        )
                            except Exception as e:
                                logger.warning(
                                    "[INSTAGRAM] Failed to parse highlights_tray for %s: %s",
                                    username,
                                    e,
                                )

            if not uid:
                logger.warning(
                    "[INSTAGRAM] Exhausted all fetch strategies for %s", username
                )
                result.error = f"⚠️ Профиль @{username} недоступен (IP Block / Скрыт)."
                return result

            # 3. Fetch stories AUTHENTICATED (because anonymous request will always fail)
            if cls._ig_cookie_pool:
                sess_idx, cookies_to_use = await cls._get_available_session()
                if cookies_to_use:
                    async with AsyncSession(
                        impersonate=cls.IMPERSONATE, cookies=cookies_to_use
                    ) as auth_session:
                        stories = await cls._fetch_reels_media(auth_session, [uid])
                        result.stories = stories.get(uid, [])

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
        cls._init_pool()
        try:
            sess_idx, cookies_to_use = await cls._get_available_session()
            if sess_idx == -2:
                return (
                    [],
                    "⏳ Инстаграм на паузе. Лимит скачиваний хайлайтов временно исчерпан.",
                )
            if not cookies_to_use:
                return (
                    [],
                    "⚠️ Скачивание хайлайтов требует настройки пула аккаунтов IG_SESSIONS_B64.",
                )

            async with AsyncSession(
                impersonate=cls.IMPERSONATE, cookies=cookies_to_use
            ) as session:
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
                    "User-Agent": cls.IG_USER_AGENT,
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
            cls._init_pool()
            sess_idx, cookies_to_use = await cls._get_available_session()
            if sess_idx == -2:
                return None, "⏳ Инстаграм на паузе. Лимит загрузок истощен. Отдыхаем."

            async with AsyncSession(
                impersonate=cls.IMPERSONATE, cookies=cookies_to_use
            ) as session:
                resp = await session.get(item.url, stream=True)
                if resp.status_code != 200:
                    return None, f"⚠️ Ошибка CDN Instagram: {resp.status_code}"

                with open(out_path, "wb") as f:
                    async for chunk in resp.aiter_content():
                        f.write(chunk)
                return out_path, None

        except Exception as e:
            logger.error("[INSTAGRAM] Download error: %s", e)
            safe_remove(out_path)
            return None, "⚠️ Внутренняя ошибка загрузки."

    @classmethod
    async def download_post(cls, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Download post/reel via Cobalt with native authenticated fallback."""
        import os
        import uuid
        from curl_cffi.requests import AsyncSession
        from app.core.config import TEMP_DIR
        from app.core.utils import safe_remove

        out_path = os.path.join(TEMP_DIR, f"ig_fallback_{uuid.uuid4().hex}.mp4")
        video_url = None

        # 1. Try public Cobalt infrastructure first (saves session bans & parses fast)
        try:
            from app.services.cobalt import CobaltService

            c_res = await CobaltService.process(url)
            if c_res and getattr(c_res, "status", None) != "picker" and c_res.url:
                video_url = c_res.url
            elif c_res and getattr(c_res, "status", None) == "picker":
                return None, "⚠️ Multi-photo карусели скачивайте через основное меню."
        except Exception as e:
            logger.warning("[INSTAGRAM] Cobalt attempt failed for post %s: %s", url, e)

        # 2. Native Fallback if Cobalt failed or returned empty URL
        if not video_url:
            logger.info(
                "[INSTAGRAM] Cobalt failed/empty for %s, triggering Native Web Fallback",
                url,
            )
            cls._init_pool()
            if not cls._ig_cookie_pool:
                return (
                    None,
                    "⚠️ Cobalt временно недоступен, а авторизация для нативной загрузки не настроена (нет IG_SESSIONS_B64).",
                )

            sess_idx, cookies_to_use = await cls._get_available_session()
            if not cookies_to_use:
                return (
                    None,
                    "⏳ Инстаграм на паузе. Лимит загрузок исчерпан на всех аккаунтах для защиты от бана. Используйте Cobalt (по умолчанию) или подождите.",
                )

            shortcode = _extract_shortcode(url)
            if not shortcode:
                return None, "⚠️ Ошибка парсинга короткой ссылки поста."

            media_pk = _shortcode_to_media_pk(shortcode)
            target_url = f"https://i.instagram.com/api/v1/media/{media_pk}/info/"

            try:
                async with AsyncSession(
                    impersonate=cls.IMPERSONATE, cookies=cookies_to_use
                ) as session:
                    doc = await session.get(
                        target_url,
                        headers={
                            "User-Agent": cls.IG_USER_AGENT,
                            "X-IG-App-ID": cls.IG_APP_ID,
                        },
                    )

                    if doc.status_code == 200:
                        data = doc.json()
                        items = data.get("items", [])

                        if items:
                            item = items[0]
                            if "video_versions" in item and item["video_versions"]:
                                video_url = item["video_versions"][0]["url"]
                            elif "carousel_media" in item:
                                return (
                                    None,
                                    "⚠️ Multi-photo карусели скачивайте через основное меню.",
                                )
                            elif (
                                "image_versions2" in item
                                and "candidates" in item["image_versions2"]
                                and item["image_versions2"]["candidates"]
                            ):
                                video_url = item["image_versions2"]["candidates"][0][
                                    "url"
                                ]
                                out_path = out_path.replace(".mp4", ".jpg")

                    if not video_url:
                        logger.warning(
                            "[INSTAGRAM] Web JSON fallback failed for %s. HTTP: %s",
                            url,
                            doc.status_code,
                        )
                        return (
                            None,
                            "⚠️ Ошибка нативного запасного канала. Вероятно, пост недоступен.",
                        )
            except Exception as e:
                logger.error(
                    "[INSTAGRAM] Web JSON fallback exception for %s: %s", url, e
                )
                return None, "⚠️ Внутренняя ошибка нативного парсера."

        # 3. Download the actual video buffer (for both Cobalt link and Native link)
        try:
            # Re-init impersonate session without cookies for pure CDN download to avoid tracking
            async with AsyncSession(impersonate=cls.IMPERSONATE) as session:
                resp = await session.get(video_url, stream=True)
                if resp.status_code != 200:
                    return (
                        None,
                        f"⚠️ Ошибка сети при скачивании медиа (CDN: {resp.status_code})",
                    )

                with open(out_path, "wb") as f:
                    async for chunk in resp.aiter_content():
                        f.write(chunk)

            return out_path, None

        except Exception as e:
            logger.error("[INSTAGRAM] Post video pipe error: %s", e)
            safe_remove(out_path)
            return None, "⚠️ Ошибка скачивания видео потока."
