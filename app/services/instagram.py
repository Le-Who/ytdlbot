"""
Instagram Service — Stories & Highlights with Rich Selection UX

Uses curl_cffi for Instagram's web and mobile APIs.
"""

import asyncio
import base64
import json
import logging
import os
import pickle
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from urllib.parse import parse_qs, unquote, urlsplit

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import HTTPError

from app.core import state
from app.core.asyncio_utils import await_owned
from app.core.config import IG_SESSIONS_B64, TEMP_DIR
from app.core.policy import max_media_file_bytes
from app.core.utils import safe_remove

__all__ = ["IGHighlight", "IGStoryItem", "InstagramService", "parse_instagram_url"]

logger = logging.getLogger("app.services.instagram")
MAX_MEDIA_BYTES = max_media_file_bytes()


async def _stream_media_response(response: Response, out_path: str) -> None:
    """Keep disk ownership until the complete bounded response is validated."""
    encoding = response.headers.get("Content-Encoding")
    identity_encoding = not isinstance(encoding, str) or encoding.strip().lower() in {
        "",
        "identity",
    }
    declared = response.headers.get("Content-Length")
    expected = (
        int(declared)
        if identity_encoding and isinstance(declared, (str, int))
        else None
    )
    if expected is not None and (expected < 0 or expected > MAX_MEDIA_BYTES):
        raise ValueError("invalid media response length")
    written = 0
    with open(out_path, "wb") as output:  # noqa: ASYNC230
        async for chunk in response.aiter_content():
            if not chunk:
                continue
            written += len(chunk)
            if written > MAX_MEDIA_BYTES:
                raise ValueError("media exceeds configured byte limit")
            await await_owned(asyncio.to_thread(output.write, chunk))
    if written == 0 or (expected is not None and written != expected):
        raise ValueError("empty or truncated media response")


# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass
class IGStoryItem:
    """A single story item (photo or video) with metadata."""

    mediaid: str
    is_video: bool
    url: str  # direct media URL
    thumbnail_url: str  # thumbnail for preview
    timestamp: datetime
    duration: float | None = None
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
    stories: list[IGStoryItem] = field(default_factory=list)
    highlights: list[IGHighlight] = field(default_factory=list)
    error: str | None = None


def _parse_reel_items(items: list[dict[str, Any]]) -> list[IGStoryItem]:
    parsed = []
    for item in items:
        is_video = item.get("media_type") == 2
        images = (item.get("image_versions2") or {}).get("candidates") or []
        videos = item.get("video_versions") or []
        sources = videos if is_video else images
        if not sources:
            continue
        url = sources[0]["url"]
        duration = float(item.get("video_duration") or 0.0)
        parsed.append(
            IGStoryItem(
                mediaid=str(item.get("pk") or item.get("id")),
                is_video=is_video,
                url=url,
                thumbnail_url=images[0]["url"] if images else url,
                timestamp=datetime.fromtimestamp(item.get("taken_at") or 0, tz=UTC),
                duration=duration if duration > 0 else None,
                typename="GraphStoryVideo" if is_video else "GraphStoryImage",
            )
        )
    return parsed


# ── URL Parsing ──────────────────────────────────────────────────────────────

_IG_PROFILE_RE = re.compile(r"/(?:@)?([A-Za-z0-9_.]+)/?")
_IG_STORIES_RE = re.compile(r"/stories/([A-Za-z0-9_.]+)(?:/([0-9]+))?/?")
_IG_HIGHLIGHT_RE = re.compile(r"/stories/highlights/([0-9]+)/?")
_IG_HIGHLIGHT_SHARE_RE = re.compile(r"/s/([A-Za-z0-9_+\-]+={0,2})/?")
_IG_POST_RE = re.compile(r"/(?:p|reel|reels)/([A-Za-z0-9_-]+)/?")


def parse_instagram_url(url: str) -> tuple[str, str | None, str | None]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return "unknown", None, None
    if host not in {"instagram.com", "instagr.am"} and not host.endswith(
        (".instagram.com", ".instagr.am")
    ):
        return "unknown", None, None

    path = unquote(parsed.path)
    highlight = _IG_HIGHLIGHT_RE.fullmatch(path)
    share = _IG_HIGHLIGHT_SHARE_RE.fullmatch(path)
    if share:
        token = share[1]
        try:
            decoded = base64.b64decode(
                token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
            )
        except ValueError:
            return "unknown", None, None
        encoded_highlight = re.fullmatch(rb"highlight:([0-9]+)", decoded)
        if not encoded_highlight:
            return "unknown", None, None
        highlight_id = encoded_highlight[1].decode("ascii")
    elif highlight:
        highlight_id = highlight[1]
    else:
        highlight_id = None

    if highlight_id:
        selector = parse_qs(parsed.query).get("story_media_id", [""])[0]
        item = re.fullmatch(r"([0-9]+)(?:_[0-9]+)?", selector)
        return "highlight", highlight_id, item[1] if item else None

    m = _IG_STORIES_RE.fullmatch(path)
    if m:
        return "stories", m.group(1), m.group(2)

    m = _IG_POST_RE.fullmatch(path)
    if m:
        return "post", m.group(1), None

    m = _IG_PROFILE_RE.fullmatch(path)
    if m:
        username = m.group(1)
        if username in (
            "stories",
            "s",
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


# ── Shortcode ↔ Media PK Conversion ─────────────────────────────────────────

_IG_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _extract_shortcode(url: str) -> str | None:
    """Extract the shortcode from an Instagram /p/, /reel/, or /reels/ URL."""
    url_type, shortcode, _ = parse_instagram_url(url)
    return shortcode if url_type == "post" else None


def _shortcode_to_media_pk(shortcode: str) -> str:
    """Convert an Instagram shortcode to numeric media PK (deterministic base64)."""
    media_pk = 0
    for char in shortcode:
        media_pk = media_pk * 64 + _IG_SHORTCODE_ALPHABET.index(char)
    return str(media_pk)


class _InstagramSessionUnavailable(RuntimeError):
    pass


# ── Core Service ─────────────────────────────────────────────────────────────


class InstagramService:
    """Fetches Instagram media with curl_cffi and configured sessions."""

    IG_APP_ID = "936619743392459"
    IMPERSONATE: Literal["chrome110"] = "chrome110"
    IG_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"

    _ig_cookie_pool: ClassVar[list[dict[str, str]]] = []
    _pool_initialized: ClassVar[bool] = False
    _current_pool_index: ClassVar[int] = 0
    _dead_sessions: ClassVar[set[int]] = set()  # Suspended/checkpointed session indices

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

            # Legacy session pickles may instantiate cookie objects with their
            # own iteration/properties; one bad session must not discard the pool.
            except Exception as e:  # noqa: BLE001
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
    ) -> tuple[int | None, dict[str, str] | None]:
        """Round-robin iterates over the session pool and claims the first allowed token from Redis limiter."""
        cls._init_pool()
        pool_size = len(cls._ig_cookie_pool)
        if pool_size == 0:
            return -1, None

        for _ in range(pool_size):
            idx = cls._current_pool_index
            cls._current_pool_index = (cls._current_pool_index + 1) % pool_size

            # Skip sessions marked as dead (suspended/checkpointed)
            if idx in cls._dead_sessions:
                continue

            # Atomic global limit check via RedisTokenBucketLimiter
            if await state.limiter.allow_ig_fallback(idx):
                return idx, cls._ig_cookie_pool[idx]

        return -2, None

    @classmethod
    def _check_response_health(
        cls, resp_status: int, resp_text: str, session_idx: int
    ) -> bool:
        """Check if an API response indicates a dead session. Returns True if session is dead."""
        if resp_status in (400, 401, 403):
            text_lower = resp_text[:500].lower() if resp_text else ""
            death_signals = (
                "checkpoint_required",
                "login_required",
                "accounts/suspended",
                "challenge_required",
                "consent_required",
            )
            if any(sig in text_lower for sig in death_signals):
                cls._dead_sessions.add(session_idx)
                alive = len(cls._ig_cookie_pool) - len(cls._dead_sessions)
                logger.warning(
                    "[INSTAGRAM] Session #%d evicted (checkpoint/suspended). %d/%d sessions alive.",
                    session_idx,
                    alive,
                    len(cls._ig_cookie_pool),
                )
                return True
        return False

    @classmethod
    def _auth_headers(cls, cookies: dict[str, str]) -> dict[str, str]:
        """Build unified authenticated request headers with CSRF token from cookie pool."""
        return {
            "User-Agent": cls.IG_USER_AGENT,
            "X-IG-App-ID": cls.IG_APP_ID,
            "X-CSRFToken": cookies.get("csrftoken", ""),
        }

    @classmethod
    @asynccontextmanager
    async def _web_page(
        cls, url: str, *, require_session: bool = True
    ) -> AsyncIterator[tuple[AsyncSession, Response, bool]]:
        for _ in range(2):
            index, cookies = await cls._get_available_session()
            if index == -2:
                raise _InstagramSessionUnavailable(
                    "Сессии Instagram временно недоступны."
                )
            if require_session and not cookies:
                raise _InstagramSessionUnavailable(
                    "Для загрузки нужна сессия Instagram."
                )
            async with AsyncSession(
                impersonate=cls.IMPERSONATE, cookies=cookies, timeout=12
            ) as session:
                page = await session.get(url, allow_redirects=False)
                if index is not None and index >= 0 and 300 <= page.status_code < 400:
                    path = urlsplit(page.headers.get("Location", "")).path
                    if path.startswith(
                        ("/consent/", "/accounts/login/", "/challenge/", "/checkpoint/")
                    ):
                        cls._dead_sessions.add(index)
                        logger.warning(
                            "[INSTAGRAM] Web session requires reauthentication"
                        )
                        continue
                yield session, page, bool(cookies)
                return
        raise _InstagramSessionUnavailable("Сессии Instagram требуют повторного входа.")

    @classmethod
    async def get_profile_media(cls, username: str) -> IGProfileMedia:
        """Fetch the profile's current stories and highlights with a web session."""
        result = IGProfileMedia(username=username)
        try:
            if not re.fullmatch(r"[A-Za-z0-9_.]+", username):
                raise ValueError("invalid Instagram username")
            async with cls._web_page(
                f"https://www.instagram.com/{username}/", require_session=False
            ) as (session, page, has_session):
                if page.status_code == 404:
                    result.error = f"Профиль @{username} не найден."
                    return result
                page.raise_for_status()
                profile_id = re.search(r'"profile_id":"([0-9]+)"', page.text)
                if not profile_id:
                    raise ValueError("Instagram profile ID unavailable")
                if not has_session:
                    result.error = (
                        "Для загрузки историй и хайлайтов нужна сессия Instagram."
                    )
                    return result
                uid = profile_id[1]
                result.stories = await cls._fetch_web_reel(session, page, uid)
                highlights = await cls._web_graphql(
                    session,
                    page,
                    "PolarisProfileStoryHighlightsTrayContentQuery",
                    "26970053832668570",
                    {"user_id": uid},
                )
                for edge in (highlights["highlights"] or {}).get("edges", ()):
                    reel = edge["node"]
                    cover = (reel.get("cover_media") or {}).get(
                        "cropped_image_version"
                    ) or {}
                    result.highlights.append(
                        IGHighlight(
                            highlight_id=str(reel["id"]).removeprefix("highlight:"),
                            title=reel["title"],
                            cover_url=cover.get("url", ""),
                            item_count=1,
                        )
                    )
        except _InstagramSessionUnavailable as error:
            result.error = str(error)
        # Keep partial results when the profile has stories but the tray is unavailable.
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "[INSTAGRAM] Profile lookup failed: %s", type(error).__name__
            )
            if not result.stories and not result.highlights:
                result.error = "Ошибка загрузки профиля Instagram."
        return result

    @classmethod
    async def get_highlight_items(
        cls, highlight_id: str
    ) -> tuple[list[IGStoryItem], str | None]:
        """Resolve a highlight with a configured Instagram session."""
        try:
            if not re.fullmatch(r"[0-9]+", highlight_id):
                raise ValueError("invalid highlight ID")
            async with cls._web_page(
                f"https://www.instagram.com/stories/highlights/{highlight_id}/"
            ) as (session, page, _):
                return await cls._fetch_web_reel(
                    session, page, f"highlight:{highlight_id}"
                ), None
        except _InstagramSessionUnavailable as error:
            return [], str(error)
        except HTTPError as error:
            status = error.response.status_code if error.response is not None else 0
            logger.warning("[INSTAGRAM] Highlight web request failed (HTTP %d)", status)
            if status == 429:
                return [], "Instagram временно ограничил загрузку хайлайтов (HTTP 429)."
            return [], "Ошибка загрузки хайлайта из Instagram."
        # Keep upstream responses and session details out of the error and logs.
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "[INSTAGRAM] Highlight items error: %s", type(error).__name__
            )
            return [], "Ошибка загрузки хайлайта из Instagram."

    @classmethod
    async def _fetch_web_reel(
        cls, session: AsyncSession, page: Response, reel_id: str
    ) -> list[IGStoryItem]:
        data = await cls._web_graphql(
            session,
            page,
            "PolarisStoriesV3HighlightsPageQuery",
            "28325328583775973",
            {
                "initial_reel_id": reel_id,
                "reel_ids": [reel_id],
                "first": 3,
                "last": 2,
                "__relay_internal__pv__PolarisCommunityNoteStoriesLabelEnabledrelayprovider": True,
            },
        )
        connection = data["xdt_api__v1__feed__reels_media__connection"] or {}
        for edge in connection.get("edges", ()):
            reel = edge["node"]
            if reel["id"] == reel_id:
                return _parse_reel_items(reel["items"])
        return []

    @classmethod
    async def _web_graphql(
        cls,
        session: AsyncSession,
        page: Response,
        operation: str,
        doc_id: str,
        variables: dict[str, Any],
    ) -> dict[str, Any]:
        page.raise_for_status()
        lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', page.text)
        if not lsd:
            raise ValueError("Instagram page did not supply an LSD token")
        dtsg = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', page.text)
        origin = "https://www.instagram.com"
        response = await session.post(
            f"{origin}/graphql/query",
            headers={
                "X-IG-App-ID": cls.IG_APP_ID,
                "User-Agent": cls.IG_USER_AGENT,
                "X-CSRFToken": session.cookies.get("csrftoken", ""),
                "X-FB-Friendly-Name": operation,
                "X-FB-LSD": lsd[1],
                "Origin": origin,
                "Referer": page.url,
            },
            data={
                "doc_id": doc_id,
                "fb_api_req_friendly_name": operation,
                "lsd": lsd[1],
                "fb_dtsg": dtsg[1] if dtsg else "",
                "variables": json.dumps(variables),
            },
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json().get("data")
        if not isinstance(data, dict):
            raise TypeError("Instagram GraphQL response has no data")
        return data

    @classmethod
    async def download_story_item(
        cls, item: IGStoryItem
    ) -> tuple[str | None, str | None]:
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
        completed = False
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

                await _stream_media_response(resp, out_path)
            completed = True
            return out_path, None

        # No partial file is returned after an arbitrary external stream failure;
        # the synchronous context manager has closed its handle before cleanup.
        except Exception as e:  # noqa: BLE001
            logger.error("[INSTAGRAM] Download error: %s", e)
            safe_remove(out_path)
            return None, "⚠️ Внутренняя ошибка загрузки."
        finally:
            if not completed:
                safe_remove(out_path)

    @classmethod
    async def download_post(cls, url: str) -> tuple[str | None, str | None]:
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
        # Cobalt is an optional provider adapter; any adapter failure must permit
        # the independent native Instagram fallback below.
        except Exception as e:  # noqa: BLE001
            logger.warning("[INSTAGRAM] Cobalt attempt failed for post %s: %s", url, e)

        # 2. Native Fallback if Cobalt failed or returned empty URL
        if not video_url:
            logger.info(
                "[INSTAGRAM] Cobalt failed/empty for %s, triggering Mobile API Fallback",
                url,
            )
            cls._init_pool()
            if not cls._ig_cookie_pool:
                return (
                    None,
                    "⚠️ Cobalt временно недоступен, а авторизация для нативной загрузки не настроена (нет IG_SESSIONS_B64).",
                )

            shortcode = _extract_shortcode(url)
            if not shortcode:
                return None, "⚠️ Ошибка парсинга короткой ссылки поста."

            media_pk = _shortcode_to_media_pk(shortcode)

            # Try both web and mobile endpoints — web is more stable with desktop cookies
            endpoints = [
                f"https://www.instagram.com/api/v1/media/{media_pk}/info/",
                f"https://i.instagram.com/api/v1/media/{media_pk}/info/",
            ]

            # Retry loop: if current session is dead, evict it and try the next one
            max_retries = len(cls._ig_cookie_pool)
            for _attempt in range(max_retries):
                sess_idx, cookies_to_use = await cls._get_available_session()
                if not cookies_to_use:
                    return (
                        None,
                        (
                            "⏳ Все аккаунты-доноры заблокированы или исчерпали лимит. Замените сессии в IG_SESSIONS_B64."
                            if sess_idx == -2 and cls._dead_sessions
                            else "⏳ Инстаграм на паузе. Лимит загрузок исчерпан на всех аккаунтах для защиты от бана."
                        ),
                    )

                try:
                    async with AsyncSession(
                        impersonate=cls.IMPERSONATE, cookies=cookies_to_use
                    ) as session:
                        doc = None
                        session_is_dead = False
                        for endpoint_url in endpoints:
                            doc = await session.get(
                                endpoint_url,
                                headers=cls._auth_headers(cookies_to_use),
                            )
                            if doc.status_code == 200:
                                break

                            # Check if this session is dead (suspended/checkpointed)
                            try:
                                body_text = doc.text[:500] if doc.text else ""
                            # Body decoding is optional session-health evidence;
                            # a broken response adapter must not mask its status.
                            except Exception:  # noqa: BLE001
                                body_text = ""

                            if sess_idx is not None and cls._check_response_health(
                                doc.status_code, body_text, sess_idx
                            ):
                                session_is_dead = True
                                break

                            # Diagnostic: log non-checkpoint errors
                            logger.warning(
                                "[INSTAGRAM] Endpoint %s returned %s. Body: %s",
                                endpoint_url,
                                doc.status_code,
                                body_text[:300] if body_text else "(empty)",
                            )

                        # If session was evicted, retry with the next available one
                        if session_is_dead:
                            continue

                        if doc and doc.status_code == 200:
                            data = doc.json()
                            items = data.get("items", [])

                            if items:
                                item = items[0]
                                if item.get("video_versions"):
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
                                    video_url = item["image_versions2"]["candidates"][
                                        0
                                    ]["url"]
                                    out_path = out_path.replace(".mp4", ".jpg")

                        if not video_url:
                            logger.warning(
                                "[INSTAGRAM] Mobile API fallback failed for %s. HTTP: %s",
                                url,
                                doc.status_code if doc else "N/A",
                            )
                            return (
                                None,
                                "⚠️ Ошибка нативного запасного канала. Вероятно, пост недоступен.",
                            )
                        # Success — break out of retry loop
                        break
                # Translate failures of native authentication/HTTP/parsing into
                # the public post-download error contract after retries finish.
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        "[INSTAGRAM] Mobile API fallback exception for %s: %s", url, e
                    )
                    return None, "⚠️ Внутренняя ошибка нативного парсера."

        # 3. Download the actual video buffer (for both Cobalt link and Native link)
        completed = False
        try:
            # Re-init impersonate session without cookies for pure CDN download to avoid tracking
            async with AsyncSession(impersonate=cls.IMPERSONATE) as session:
                resp = await session.get(video_url, stream=True)
                if resp.status_code != 200:
                    return (
                        None,
                        f"⚠️ Ошибка сети при скачивании медиа (CDN: {resp.status_code})",
                    )

                await _stream_media_response(resp, out_path)
            completed = True
            return out_path, None

        # Arbitrary CDN stream failures must remove the closed partial file and
        # return an error rather than publish an incomplete video path.
        except Exception as e:  # noqa: BLE001
            logger.error("[INSTAGRAM] Post video pipe error: %s", e)
            safe_remove(out_path)
            return None, "⚠️ Ошибка скачивания видео потока."
        finally:
            if not completed:
                safe_remove(out_path)
