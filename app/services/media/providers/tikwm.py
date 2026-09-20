"""Resolution-only adapter for the public TikWM endpoint."""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import quote

from ..models import MediaCandidate, MediaItem, MediaKind, MediaRequest, MediaSource
from ..registry import FailureKind, ProviderError
from .http import (
    CurlProviderTransport,
    HttpTransport,
    ProviderEndpoint,
    endpoint_headers,
    probe_candidate,
    request_json,
)

DEFAULT_ENDPOINT = ProviderEndpoint(
    "https://www.tikwm.com", None, frozenset({"tiktok"}), True
)


class TikWMProvider:
    name = "tikwm"
    backend_family = "tikwm-http"
    is_heavy = False

    def __init__(
        self,
        endpoint: ProviderEndpoint = DEFAULT_ENDPOINT,
        *,
        transport: HttpTransport | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.endpoint = endpoint
        self._transport = transport or CurlProviderTransport()
        self._wall_clock = wall_clock

    def supports(self, request: MediaRequest) -> bool:
        return self.endpoint.supports(request.platform)

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        api_url = (
            f"{self.endpoint.origin}/api/?url="
            f"{quote(request.canonical_url, safe='')}&hd=1"
        )
        data, _ = await request_json(
            self._transport,
            "GET",
            api_url,
            headers=endpoint_headers(self.endpoint),
        )
        code = data.get("code")
        if code != 0:
            message = str(data.get("msg") or "unknown error")
            kind = (
                FailureKind.TRANSIENT
                if code == -1 and any(word in message.lower() for word in ("limit", "many"))
                else FailureKind.PERMANENT
            )
            raise ProviderError(kind, f"TikWM: {message}")
        info = data.get("data")
        if not isinstance(info, dict):
            raise ProviderError(FailureKind.INTERNAL, "TikWM omitted data")
        images = info.get("images")
        if isinstance(images, list) and images:
            return [await self._album_candidate(request, info, images)]
        return [await self._video_candidate(request, info)]

    async def _album_candidate(
        self, request: MediaRequest, info: dict[str, object], images: list[object]
    ) -> MediaCandidate:
        items: list[MediaItem] = []
        sources: list[MediaSource] = []
        for index, raw_url in enumerate(images):
            if not isinstance(raw_url, str) or not raw_url:
                raise ProviderError(FailureKind.INTERNAL, "TikWM album item omitted URL")
            await probe_candidate(
                self._transport, raw_url, wall_clock=self._wall_clock
            )
            items.append(
                MediaItem(f"{request.media_id}:{index}", MediaKind.PHOTO, raw_url)
            )
            sources.append(MediaSource(str(index), raw_url, container="image"))
        music = info.get("music")
        if isinstance(music, str) and music:
            await probe_candidate(
                self._transport, music, wall_clock=self._wall_clock
            )
            sources.append(MediaSource("audio", music, audio_codec="unknown"))
        return MediaCandidate(
            candidate_id="tikwm:album",
            url=items[0].url,
            has_video=False,
            has_audio=isinstance(music, str) and bool(music),
            sources=tuple(sources),
            complete=True,
            provider=self.name,
            backend_family=self.backend_family,
            media_id=request.media_id,
            kind=MediaKind.ALBUM,
            items=tuple(items),
            metadata_complete=False,
            auth_scope=request.auth_scope,
        )

    async def _video_candidate(
        self, request: MediaRequest, info: dict[str, object]
    ) -> MediaCandidate:
        hd_url = info.get("hdplay")
        sd_url = info.get("play")
        url = hd_url if isinstance(hd_url, str) and hd_url else sd_url
        if not isinstance(url, str) or not url:
            raise ProviderError(FailureKind.PERMANENT, "TikWM returned no media")
        await probe_candidate(self._transport, url, wall_clock=self._wall_clock)
        width = _integer(info.get("width"))
        height = _integer(info.get("height"))
        quality_limited = _quality_limited(request, width, height)
        size_key = "hd_size" if url == hd_url else "size"
        size = _integer(info.get(size_key))
        item = MediaItem(
            request.media_id,
            MediaKind.VIDEO,
            url,
            duration_seconds=_number(info.get("duration")),
            width=width,
            height=height,
            container="mp4",
            filesize_bytes=size,
        )
        return MediaCandidate(
            candidate_id="tikwm:hd" if url == hd_url else "tikwm:sd",
            url=url,
            width=width,
            height=height,
            has_video=True,
            has_audio=True,
            container="mp4",
            filesize_bytes=size,
            duration_seconds=item.duration_seconds,
            sources=(MediaSource("direct", url, container="mp4", filesize_bytes=size),),
            provider=self.name,
            backend_family=self.backend_family,
            media_id=request.media_id,
            kind=MediaKind.VIDEO,
            items=(item,),
            quality_label="hd" if url == hd_url else "sd",
            quality_limited=quality_limited,
            metadata_complete=width is not None and height is not None,
            auth_scope=request.auth_scope,
        )


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


def _quality_limited(
    request: MediaRequest, width: int | None, height: int | None
) -> bool:
    requested = request.quality.max_edge
    if requested is None or width is None or height is None:
        return False
    return min(width, height) < requested
