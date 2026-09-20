"""Local gallery-dl metadata adapter for ordered public albums and media."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from app.core.process import run_subprocess

from ..models import MediaCandidate, MediaItem, MediaKind, MediaRequest, MediaSource
from ..registry import FailureKind, ProviderError

GalleryExtractor = Callable[[str], Awaitable[Sequence[object]]]

_PLATFORMS = frozenset({"tiktok", "instagram", "facebook", "pinterest"})
_KINDS = frozenset(
    {
        MediaKind.AUTO,
        MediaKind.VIDEO,
        MediaKind.PHOTO,
        MediaKind.ANIMATION,
        MediaKind.ALBUM,
    }
)
_VIDEO_EXTENSIONS = frozenset({"m4v", "mkv", "mov", "mp4", "webm"})
_ANIMATION_EXTENSIONS = frozenset({"gif"})
_PHOTO_EXTENSIONS = frozenset({"avif", "bmp", "jpeg", "jpg", "png", "webp"})
_SAFE_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "origin", "referer", "user-agent"}
)


class GalleryDlProvider:
    name = "gallery-dl"
    backend_family = "local-gallery-dl"
    is_heavy = True

    def __init__(self, *, extract: GalleryExtractor | None = None) -> None:
        self._extract = extract or _dump_gallery
        self.available = (
            extract is not None or importlib.util.find_spec("gallery_dl") is not None
        )

    def supports(self, request: MediaRequest) -> bool:
        return (
            self.available
            and request.auth_scope == "public"
            and request.platform in _PLATFORMS
            and request.kind in _KINDS
        )

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        messages = await self._extract(request.canonical_url)
        items: list[MediaItem] = []
        sources: list[MediaSource] = []
        directory: Mapping[str, Any] = {}
        for message in messages:
            if not isinstance(message, (list, tuple)) or not message:
                continue
            if message[0] == 2 and len(message) >= 2 and isinstance(message[-1], dict):
                directory = message[-1]
                continue
            if message[0] != 3 or len(message) < 3:
                continue
            url, metadata = message[1], message[2]
            if not isinstance(url, str) or not isinstance(metadata, dict):
                continue
            kind, container = _kind_and_container(url, metadata)
            if kind is None:
                continue
            index = len(items)
            media_id = str(
                metadata.get("id")
                or metadata.get("media_id")
                or metadata.get("filename")
                or f"{request.media_id}:{index}"
            )
            width = _positive_int(metadata.get("width"))
            height = _positive_int(metadata.get("height"))
            duration = _nonnegative_float(metadata.get("duration"))
            size = _positive_int(metadata.get("filesize") or metadata.get("file_size"))
            title = metadata.get("title") or directory.get("title")
            items.append(
                MediaItem(
                    media_id=media_id,
                    kind=kind,
                    url=url,
                    title=str(title) if title else None,
                    duration_seconds=duration,
                    width=width,
                    height=height,
                    container=container,
                    filesize_bytes=size,
                )
            )
            sources.append(
                MediaSource(
                    format_id=str(index),
                    url=url,
                    container=container,
                    filesize_bytes=size,
                    http_headers=_safe_headers(metadata),
                )
            )
        if not items:
            raise ProviderError(FailureKind.PERMANENT, "gallery-dl found no media")
        kind = items[0].kind if len(items) == 1 else MediaKind.ALBUM
        if request.kind is not MediaKind.AUTO and request.kind is not kind:
            return []
        first = items[0]
        return [
            MediaCandidate(
                candidate_id="gallery:" + ":".join(item.media_id for item in items),
                url=first.url,
                width=first.width if len(items) == 1 else None,
                height=first.height if len(items) == 1 else None,
                has_video=any(
                    item.kind in {MediaKind.VIDEO, MediaKind.ANIMATION}
                    for item in items
                ),
                has_audio=any(item.kind is MediaKind.VIDEO for item in items),
                container=first.container if len(items) == 1 else None,
                filesize_bytes=first.filesize_bytes if len(items) == 1 else None,
                duration_seconds=first.duration_seconds if len(items) == 1 else None,
                sources=tuple(sources),
                complete=True,
                provider=self.name,
                backend_family=self.backend_family,
                media_id=request.media_id,
                kind=kind,
                items=tuple(items),
                auth_scope="public",
            )
        ]


async def _dump_gallery(url: str) -> Sequence[object]:
    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--dump-json",
        "--no-input",
        "--",
        url,
    ]
    try:
        async with run_subprocess(command) as handle:
            if handle.proc.stdout is None:
                raise ProviderError(
                    FailureKind.INTERNAL, "gallery-dl stdout unavailable"
                )
            stdout = await handle.proc.stdout.read()
            return_code = await handle.wait()
    except OSError as error:
        raise ProviderError(FailureKind.CONFIG, "gallery-dl is unavailable") from error
    if return_code != 0:
        raise ProviderError(FailureKind.TRANSIENT, "gallery-dl extraction failed")
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ProviderError(
            FailureKind.INTERNAL, "gallery-dl returned invalid data"
        ) from error
    if not isinstance(value, list):
        raise ProviderError(FailureKind.INTERNAL, "gallery-dl returned invalid data")
    return value


def _kind_and_container(
    url: str, metadata: Mapping[str, Any]
) -> tuple[MediaKind | None, str | None]:
    raw_extension = metadata.get("extension") or metadata.get("ext")
    if raw_extension:
        extension = str(raw_extension).lower().removeprefix(".")
    else:
        extension = PurePosixPath(urlsplit(url).path).suffix.lower().removeprefix(".")
    if extension in _VIDEO_EXTENSIONS:
        return MediaKind.VIDEO, extension
    if extension in _ANIMATION_EXTENSIONS:
        return MediaKind.ANIMATION, extension
    if extension in _PHOTO_EXTENSIONS:
        return MediaKind.PHOTO, extension
    return None, extension or None


def _safe_headers(metadata: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = metadata.get("http_headers") or metadata.get("_http_headers")
    if not isinstance(raw, Mapping):
        return ()
    headers: list[tuple[str, str]] = []
    for name, value in raw.items():
        normalized = str(name).strip().lower()
        text = str(value).strip()
        if (
            normalized in _SAFE_HEADERS
            and "\r" not in normalized
            and "\n" not in normalized
            and "\r" not in text
            and "\n" not in text
        ):
            headers.append((normalized, text))
    return tuple(sorted(headers))


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


__all__ = ["GalleryDlProvider"]
