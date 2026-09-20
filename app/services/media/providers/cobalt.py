"""Resolution-only adapter for explicitly configured Cobalt origins."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

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


class CobaltProvider:
    name = "cobalt"
    backend_family = "cobalt-http"
    is_heavy = False

    def __init__(
        self,
        endpoints: Sequence[ProviderEndpoint],
        *,
        transport: HttpTransport | None = None,
        pacer: OriginPacing | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.endpoints = tuple(endpoints)
        self._transport = transport or CurlProviderTransport()
        self._pacer = pacer or OriginPacer()
        self._wall_clock = wall_clock

    def supports(self, request: MediaRequest) -> bool:
        return any(endpoint.supports(request.platform) for endpoint in self.endpoints)

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        return await self.resolve_with(self._transport, request)

    async def resolve_with(
        self, transport: HttpTransport, request: MediaRequest
    ) -> list[MediaCandidate]:
        last_error: ProviderError | None = None
        attempted = False
        for endpoint in self.endpoints:
            if not endpoint.supports(request.platform):
                continue
            attempted = True
            try:
                return await self._resolve_endpoint(transport, endpoint, request)
            except ProviderError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        if attempted:
            raise ProviderError(FailureKind.PERMANENT, "Cobalt returned no candidate")
        return []

    async def _resolve_endpoint(
        self,
        transport: HttpTransport,
        endpoint: ProviderEndpoint,
        request: MediaRequest,
    ) -> list[MediaCandidate]:
        payload: dict[str, object] = {
            "url": request.canonical_url,
            "filenameStyle": "basic",
        }
        if request.quality.max_edge is not None:
            payload["videoQuality"] = str(request.quality.max_edge)
        await self._pacer.wait(endpoint.origin, endpoint.min_interval)
        data, _ = await request_json(
            transport,
            "POST",
            f"{endpoint.origin}/",
            headers=endpoint_headers(endpoint),
            json=payload,
        )
        status = data.get("status")
        if status == "error":
            error_data = data.get("error")
            code = error_data.get("code") if isinstance(error_data, dict) else None
            raise ProviderError(
                FailureKind.PERMANENT,
                f"Cobalt error: {code or 'unknown_error'}",
            )
        if status in {"redirect", "tunnel"}:
            url = data.get("url")
            if not isinstance(url, str) or not url:
                raise ProviderError(FailureKind.INTERNAL, "Cobalt omitted media URL")
            await probe_candidate(transport, url, wall_clock=self._wall_clock)
            item = MediaItem(request.media_id, MediaKind.VIDEO, url)
            return [
                MediaCandidate(
                    candidate_id=f"{endpoint.origin}:{status}",
                    url=url,
                    sources=(MediaSource("direct", url),),
                    provider=self.name,
                    backend_family=self.backend_family,
                    media_id=request.media_id,
                    kind=MediaKind.VIDEO,
                    items=(item,),
                    metadata_complete=False,
                    auth_scope=request.auth_scope,
                )
            ]
        if status == "picker":
            raw_picker = data.get("picker")
            if not isinstance(raw_picker, list) or not raw_picker:
                raise ProviderError(FailureKind.INTERNAL, "Cobalt picker is empty")
            items: list[MediaItem] = []
            sources: list[MediaSource] = []
            for index, raw_item in enumerate(raw_picker):
                if not isinstance(raw_item, dict):
                    raise ProviderError(
                        FailureKind.INTERNAL, "Cobalt picker item is malformed"
                    )
                url = raw_item.get("url")
                if not isinstance(url, str) or not url:
                    raise ProviderError(
                        FailureKind.INTERNAL, "Cobalt picker item omitted URL"
                    )
                await probe_candidate(transport, url, wall_clock=self._wall_clock)
                kind = _cobalt_kind(raw_item.get("type"))
                items.append(
                    MediaItem(f"{request.media_id}:{index}", kind, url)
                )
                sources.append(MediaSource(str(index), url))
            audio = data.get("audio")
            if isinstance(audio, str) and audio:
                await probe_candidate(
                    transport, audio, wall_clock=self._wall_clock
                )
                sources.append(MediaSource("audio", audio, audio_codec="unknown"))
            return [
                MediaCandidate(
                    candidate_id=f"{endpoint.origin}:picker",
                    url=items[0].url,
                    has_video=any(
                        item.kind in {MediaKind.VIDEO, MediaKind.ANIMATION}
                        for item in items
                    ),
                    has_audio=bool(audio),
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
            ]
        raise ProviderError(
            FailureKind.PERMANENT, f"unsupported Cobalt status: {status!r}"
        )


def _cobalt_kind(value: object) -> MediaKind:
    if value == "photo":
        return MediaKind.PHOTO
    if value == "gif":
        return MediaKind.ANIMATION
    return MediaKind.VIDEO
