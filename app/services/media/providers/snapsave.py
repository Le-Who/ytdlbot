"""Disabled-by-default SnapSave web adapter with explicit job state."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from html.parser import HTMLParser

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
    request_html,
)

DEFAULT_ENDPOINT = ProviderEndpoint(
    "https://snapsave.app",
    None,
    frozenset({"facebook", "instagram"}),
    True,
)


class UpstreamRenderRequired(ProviderError):
    """The web endpoint created a remote render job, not a ready media link."""

    def __init__(self, job_id: str) -> None:
        super().__init__(FailureKind.TRANSIENT, "SnapSave upstream render pending")
        self.job_id = job_id


class SnapSaveProvider:
    name = "snapsave"
    backend_family = "snapsave-web"
    is_heavy = False

    def __init__(
        self,
        endpoint: ProviderEndpoint = DEFAULT_ENDPOINT,
        *,
        transport: HttpTransport | None = None,
        pacer: OriginPacing | None = None,
        contract_verified: bool = False,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.endpoint = endpoint
        self.contract_verified = contract_verified
        self._transport = transport or CurlProviderTransport()
        self._pacer = pacer or OriginPacer()
        self._wall_clock = wall_clock

    def supports(self, request: MediaRequest) -> bool:
        return self.contract_verified and self.endpoint.supports(request.platform)

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        await self._pacer.wait(self.endpoint.origin, self.endpoint.min_interval)
        result = await request_html(
            self._transport,
            "POST",
            f"{self.endpoint.origin}/",
            headers=endpoint_headers(self.endpoint),
            data={"url": request.canonical_url},
        )
        parser = _SnapSaveParser()
        parser.feed(result.text)
        if parser.job_id:
            raise UpstreamRenderRequired(parser.job_id)
        if not parser.links:
            raise ProviderError(FailureKind.TRANSIENT, "SnapSave returned no ready link")
        candidates: list[MediaCandidate] = []
        for index, (url, quality) in enumerate(parser.links):
            await probe_candidate(
                self._transport, url, wall_clock=self._wall_clock
            )
            kind = _kind_from_url(url)
            label = _normalized_quality(quality)
            quality_limited = _is_lower_quality(request, label)
            item = MediaItem(request.media_id, kind, url, container=_extension(url))
            candidates.append(
                MediaCandidate(
                    candidate_id=f"snapsave:{index}",
                    url=url,
                    has_video=kind in {MediaKind.VIDEO, MediaKind.ANIMATION},
                    has_audio=kind is MediaKind.VIDEO,
                    container=item.container,
                    sources=(MediaSource(str(index), url, container=item.container),),
                    provider=self.name,
                    backend_family=self.backend_family,
                    media_id=request.media_id,
                    kind=kind,
                    items=(item,),
                    quality_label=label,
                    quality_limited=quality_limited,
                    metadata_complete=False,
                    remote_processing=False,
                    auth_scope=request.auth_scope,
                )
            )
        return candidates


class _SnapSaveParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.job_id: str | None = None
        self._href: str | None = None
        self._quality = ""
        self._label: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        job_id = values.get("data-job-id")
        if job_id and (values.get("data-status") or "").lower() not in {
            "ready",
            "done",
        }:
            self.job_id = job_id
        if tag != "a":
            return
        href = values.get("href")
        classes = (values.get("class") or "").lower().split()
        is_download = (
            "download" in values
            or "data-quality" in values
            or any("download" in class_name for class_name in classes)
        )
        if href and is_download and href.startswith(("http://", "https://")):
            self._href = href
            self._quality = values.get("data-quality") or ""
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            quality = self._quality or " ".join(self._label)
            self.links.append((self._href, quality.strip()))
            self._href = None
            self._quality = ""
            self._label = []


def _normalized_quality(value: str) -> str | None:
    match = re.search(r"\b(\d{3,4})p?\b", value, re.IGNORECASE)
    return f"{match.group(1)}p" if match else None


def _is_lower_quality(request: MediaRequest, quality: str | None) -> bool:
    requested = request.quality.max_edge
    if requested is None or quality is None:
        return False
    return int(quality.removesuffix("p")) < requested


def _kind_from_url(url: str) -> MediaKind:
    extension = _extension(url)
    if extension in {"jpg", "jpeg", "png", "webp"}:
        return MediaKind.PHOTO
    if extension == "gif":
        return MediaKind.ANIMATION
    return MediaKind.VIDEO


def _extension(url: str) -> str | None:
    path = url.split("?", 1)[0]
    return path.rsplit(".", 1)[-1].lower() if "." in path else None
