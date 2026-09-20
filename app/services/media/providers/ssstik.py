"""Fixture-tested SSSTik web adapter with dynamic form-token discovery."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from urllib.parse import urljoin

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
    same_origin,
)

DEFAULT_ENDPOINT = ProviderEndpoint(
    "https://ssstik.io", None, frozenset({"tiktok"}), True
)


class SSSTikProvider:
    name = "ssstik"
    backend_family = "ssstik-web"
    is_heavy = False

    def __init__(
        self,
        endpoint: ProviderEndpoint = DEFAULT_ENDPOINT,
        *,
        transport: HttpTransport | None = None,
        pacer: OriginPacing | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.endpoint = endpoint
        self._transport = transport or CurlProviderTransport()
        self._pacer = pacer or OriginPacer()
        self._wall_clock = wall_clock

    def supports(self, request: MediaRequest) -> bool:
        return self.endpoint.supports(request.platform)

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        headers = endpoint_headers(self.endpoint)
        await self._pacer.wait(self.endpoint.origin, self.endpoint.min_interval)
        index = await request_html(
            self._transport, "GET", f"{self.endpoint.origin}/en-1", headers=headers
        )
        form = _FormParser()
        form.feed(index.text)
        if not form.action or not form.hidden:
            raise ProviderError(FailureKind.TRANSIENT, "SSSTik live form token missing")
        payload = dict(form.hidden)
        payload.setdefault("locale", "en")
        payload[form.url_field or "id"] = request.canonical_url
        post_url = urljoin(f"{self.endpoint.origin}/", form.action)
        if not same_origin(self.endpoint.origin, post_url):
            raise ProviderError(
                FailureKind.CONFIG, "SSSTik form action changed origin"
            )
        cookie = _session_cookie(index.headers)
        post_headers = dict(headers)
        if cookie:
            post_headers["Cookie"] = cookie
        await self._pacer.wait(self.endpoint.origin, self.endpoint.min_interval)
        result = await request_html(
            self._transport,
            "POST",
            post_url,
            headers=post_headers,
            data=payload,
        )
        parser = _DownloadParser()
        parser.feed(result.text)
        if not parser.links:
            raise ProviderError(FailureKind.TRANSIENT, "SSSTik returned no ready link")
        candidates: list[MediaCandidate] = []
        for index_number, (url, label) in enumerate(parser.links):
            await probe_candidate(
                self._transport, url, wall_clock=self._wall_clock
            )
            quality = _quality_label(label)
            kind = _kind_from_url(url)
            item = MediaItem(request.media_id, kind, url, container=_extension(url))
            candidates.append(
                MediaCandidate(
                    candidate_id=f"ssstik:{index_number}",
                    url=url,
                    has_video=kind in {MediaKind.VIDEO, MediaKind.ANIMATION},
                    has_audio=kind is MediaKind.VIDEO,
                    container=item.container,
                    sources=(MediaSource(str(index_number), url, container=item.container),),
                    provider=self.name,
                    backend_family=self.backend_family,
                    media_id=request.media_id,
                    kind=kind,
                    items=(item,),
                    quality_label=quality,
                    metadata_complete=False,
                    auth_scope=request.auth_scope,
                    watermark_free=_watermark_free(label),
                )
            )
        return candidates


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action: str | None = None
        self.hidden: dict[str, str] = {}
        self.url_field: str | None = None
        self._in_form = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag == "form" and self.action is None:
            self._in_form = True
            self.action = values.get("action")
        elif tag == "input" and self._in_form:
            name = values.get("name")
            if not name:
                return
            if (values.get("type") or "text").lower() == "hidden":
                self.hidden[name] = values.get("value") or ""
            elif self.url_field is None:
                self.url_field = name

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_form:
            self._in_form = False


class _DownloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        classes = (values.get("class") or "").lower().split()
        is_download = (
            "download" in values
            or "data-directurl" in values
            or any(
                "download" in class_name or "watermark" in class_name
                for class_name in classes
            )
        )
        if href and is_download and href.startswith(("http://", "https://")):
            self._href = href
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label).strip()))
            self._href = None
            self._label = []


def _session_cookie(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() == "set-cookie":
            return value.split(";", 1)[0]
    return None


def _quality_label(label: str) -> str | None:
    match = re.search(r"\b(\d{3,4})p?\b", label, re.IGNORECASE)
    return f"{match.group(1)}p" if match else None


def _watermark_free(label: str) -> bool | None:
    lowered = label.lower()
    if "without watermark" in lowered or "no watermark" in lowered:
        return True
    if "watermark" in lowered:
        return False
    return None


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
