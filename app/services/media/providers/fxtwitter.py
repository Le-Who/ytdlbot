"""FxTwitter API v2 adapter preserving attachment order and variants."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from ..models import MediaCandidate, MediaItem, MediaKind, MediaRequest, MediaSource
from ..registry import FailureKind, ProviderError
from .http import (
    CurlProviderTransport,
    HttpTransport,
    OriginPacer,
    OriginPacing,
    ProviderEndpoint,
    endpoint_headers,
    probe_candidate,
    request_json,
)

DEFAULT_ENDPOINT = ProviderEndpoint(
    "https://api.fxtwitter.com",
    None,
    frozenset({"twitter", "x"}),
    True,
    min_interval=0.1,
)


class FxTwitterProvider:
    name = "fxtwitter"
    backend_family = "fxtwitter-http"
    is_heavy = False

    def __init__(
        self,
        endpoint: ProviderEndpoint = DEFAULT_ENDPOINT,
        *,
        transport: HttpTransport | None = None,
        min_interval: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pacer: OriginPacing | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._transport = transport or CurlProviderTransport()
        self._min_interval = (
            endpoint.min_interval if min_interval is None else max(0.0, min_interval)
        )
        self._pacer = pacer or OriginPacer(clock=monotonic, sleep=sleep)

    def supports(self, request: MediaRequest) -> bool:
        return self.endpoint.supports(request.platform)

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        await self._wait_for_rate_slot()
        url = f"{self.endpoint.origin}/2/status/{quote(request.media_id, safe='')}"
        data, _ = await request_json(
            self._transport,
            "GET",
            url,
            headers=endpoint_headers(self.endpoint),
        )
        code = data.get("code")
        if code != 200:
            failure_kind = (
                FailureKind.TRANSIENT
                if code in {429, 500, 502, 503, 504}
                else FailureKind.PERMANENT
            )
            raise ProviderError(
                failure_kind, f"FxTwitter status error: {code!r}"
            )
        status = data.get("status")
        if not isinstance(status, dict):
            raise ProviderError(FailureKind.PERMANENT, "FxTwitter status is missing")
        media = status.get("media")
        if not isinstance(media, dict):
            raise ProviderError(FailureKind.PERMANENT, "FxTwitter status has no media")
        raw_items, order_complete = _ordered_media(media)
        if not raw_items:
            raise ProviderError(FailureKind.PERMANENT, "FxTwitter status has no media")

        items: list[MediaItem] = []
        sources: list[MediaSource] = []
        for index, raw in enumerate(raw_items):
            item, source = _media_item(request.media_id, index, raw)
            await probe_candidate(self._transport, item.url)
            items.append(item)
            sources.append(source)

        candidate_kind = items[0].kind if len(items) == 1 else MediaKind.ALBUM
        width = items[0].width if len(items) == 1 else None
        height = items[0].height if len(items) == 1 else None
        quality_limited = any(
            _item_quality_limited(request, item)
            for item in items
            if item.kind is MediaKind.VIDEO
        )
        return [
            MediaCandidate(
                candidate_id=f"fxtwitter:{request.media_id}",
                url=items[0].url,
                width=width,
                height=height,
                has_video=any(
                    item.kind in {MediaKind.VIDEO, MediaKind.ANIMATION}
                    for item in items
                ),
                has_audio=any(item.kind is MediaKind.VIDEO for item in items),
                container=items[0].container if len(items) == 1 else None,
                filesize_bytes=items[0].filesize_bytes if len(items) == 1 else None,
                duration_seconds=items[0].duration_seconds if len(items) == 1 else None,
                sources=tuple(sources),
                complete=order_complete,
                provider=self.name,
                backend_family=self.backend_family,
                media_id=request.media_id,
                kind=candidate_kind,
                items=tuple(items),
                quality_limited=quality_limited,
                metadata_complete=all(
                    item.width is not None and item.height is not None for item in items
                ),
                auth_scope=request.auth_scope,
            )
        ]

    async def _wait_for_rate_slot(self) -> None:
        await self._pacer.wait(self.endpoint.origin, self._min_interval)


def _ordered_media(media: dict[str, object]) -> tuple[list[dict[str, object]], bool]:
    all_items = media.get("all")
    if isinstance(all_items, list):
        return [item for item in all_items if isinstance(item, dict)], True
    photos = media.get("photos")
    videos = media.get("videos")
    photo_items = [item for item in photos if isinstance(item, dict)] if isinstance(photos, list) else []
    video_items = [item for item in videos if isinstance(item, dict)] if isinstance(videos, list) else []
    return photo_items + video_items, not (photo_items and video_items)


def _media_item(
    media_id: str, index: int, raw: dict[str, object]
) -> tuple[MediaItem, MediaSource]:
    kind = _kind(raw.get("type"))
    selected = raw
    formats = raw.get("formats")
    if isinstance(formats, list):
        usable = [item for item in formats if isinstance(item, dict) and item.get("url")]
        if usable:
            selected = max(usable, key=_format_rank)
    url = selected.get("url")
    if not isinstance(url, str) or not url:
        raise ProviderError(FailureKind.INTERNAL, "FxTwitter media omitted URL")
    width = _integer(selected.get("width")) or _integer(raw.get("width"))
    height = _integer(selected.get("height")) or _integer(raw.get("height"))
    container = _container(selected.get("container") or raw.get("format"), url)
    size = _integer(selected.get("size")) or _integer(raw.get("filesize"))
    duration = _number(raw.get("duration"))
    item = MediaItem(
        f"{media_id}:{index}",
        kind,
        url,
        duration_seconds=duration,
        width=width,
        height=height,
        container=container,
        filesize_bytes=size,
    )
    source = MediaSource(
        str(index),
        url,
        video_codec=str(selected.get("codec")) if selected.get("codec") else None,
        container=container,
        filesize_bytes=size,
    )
    return item, source


def _format_rank(raw: dict[str, object]) -> tuple[int, int, int]:
    width = _integer(raw.get("width")) or 0
    height = _integer(raw.get("height")) or 0
    bitrate = _integer(raw.get("bitrate")) or 0
    return min(width, height), width * height, bitrate


def _kind(value: object) -> MediaKind:
    if value == "photo":
        return MediaKind.PHOTO
    if value == "gif":
        return MediaKind.ANIMATION
    return MediaKind.VIDEO


def _container(value: object, url: str) -> str | None:
    if isinstance(value, str) and value:
        return value.split("/")[-1]
    path = url.split("?", 1)[0]
    return path.rsplit(".", 1)[-1] if "." in path else None


def _integer(value: object) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _item_quality_limited(request: MediaRequest, item: MediaItem) -> bool:
    requested = request.quality.max_edge
    if requested is None or item.width is None or item.height is None:
        return False
    return min(item.width, item.height) < requested
