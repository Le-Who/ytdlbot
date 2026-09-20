"""Immutable contracts shared by media providers and delivery code."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self
from urllib.parse import parse_qs, quote, urlsplit


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    ALBUM = "album"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """The requested short display edge for a video, if one is required."""

    max_edge: int | None = None


@dataclass(frozen=True, slots=True)
class ClipInterval:
    start_seconds: float | None = None
    end_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.start_seconds is not None and self.start_seconds < 0:
            raise ValueError("clip start must not be negative")
        if self.end_seconds is not None and self.end_seconds < 0:
            raise ValueError("clip end must not be negative")
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("clip end must not precede clip start")


class UnsupportedMediaUrlError(ValueError):
    """Raised when a URL cannot be represented by this provider contract."""


@dataclass(frozen=True, slots=True)
class MediaRequest:
    canonical_url: str
    platform: str
    media_id: str
    kind: MediaKind = MediaKind.VIDEO
    quality: QualityPolicy = field(default_factory=QualityPolicy)
    audio_format: str | None = None
    audio_language: str | None = None
    clip: ClipInterval = field(default_factory=ClipInterval)
    album_selection: tuple[int, ...] = ()
    watermark_allowed: bool = True
    caller_scope: str = "public"
    auth_scope: str = "public"
    exact: bool = True
    deadline: float | None = None

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        kind: MediaKind = MediaKind.VIDEO,
        quality: QualityPolicy | None = None,
        audio_format: str | None = None,
        audio_language: str | None = None,
        clip: ClipInterval | None = None,
        album_selection: tuple[int, ...] = (),
        watermark_allowed: bool = True,
        caller_scope: str = "public",
        auth_scope: str = "public",
        exact: bool = True,
        deadline: float | None = None,
    ) -> Self:
        canonical_url, platform, media_id, parsed_clip = _parse_media_url(url)
        return cls(
            canonical_url=canonical_url,
            platform=platform,
            media_id=media_id,
            kind=kind,
            quality=quality or QualityPolicy(),
            audio_format=audio_format,
            audio_language=audio_language,
            clip=clip or parsed_clip,
            album_selection=album_selection,
            watermark_allowed=watermark_allowed,
            caller_scope=caller_scope,
            auth_scope=auth_scope,
            exact=exact,
            deadline=deadline,
        )

    @property
    def cache_key(self) -> str:
        return request_cache_key(self)


@dataclass(frozen=True, slots=True)
class MediaItem:
    media_id: str
    kind: MediaKind
    url: str
    title: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    candidate_id: str
    url: str
    width: int | None = None
    height: int | None = None
    has_video: bool = True
    has_audio: bool = True
    audio_formats: tuple[str, ...] = ()
    audio_languages: tuple[str, ...] = ()
    container: str | None = None
    filesize_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedMedia:
    request: MediaRequest
    items: tuple[MediaItem, ...]
    candidates: tuple[MediaCandidate, ...] = ()
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    destination: str
    caller_scope: str = "public"
    auth_scope: str = "public"


@dataclass(frozen=True, slots=True)
class DeliveredItem:
    item: MediaItem
    status: DeliveryStatus
    delivery_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    target: DeliveryTarget
    items: tuple[DeliveredItem, ...]
    status: DeliveryStatus


def canonicalize_media_url(url: str) -> str:
    """Return the tracking-free YouTube watch URL for a supported media URL."""
    return _parse_media_url(url)[0]


def request_cache_key(request: MediaRequest) -> str:
    """Build a stable key only from fields that affect resolved media output."""
    payload = {
        "platform": request.platform,
        "media_id": request.media_id,
        "canonical_url": request.canonical_url,
        "kind": request.kind.value,
        "quality_max_edge": request.quality.max_edge,
        "audio_format": request.audio_format,
        "audio_language": request.audio_language,
        "clip_start_seconds": request.clip.start_seconds,
        "clip_end_seconds": request.clip.end_seconds,
        "album_selection": request.album_selection,
        "watermark_allowed": request.watermark_allowed,
        "caller_scope": request.caller_scope,
        "auth_scope": request.auth_scope,
        "exact": request.exact,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=list)
    return f"media:v1:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _parse_media_url(url: str) -> tuple[str, str, str, ClipInterval]:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise UnsupportedMediaUrlError("malformed media URL") from error

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise UnsupportedMediaUrlError("media URL must use HTTP(S) and include a host")

    host = hostname.lower().rstrip(".")
    query = parse_qs(parsed.query, keep_blank_values=True)
    path_parts = [part for part in parsed.path.split("/") if part]

    media_id: str | None = None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if path_parts[:1] == ["watch"]:
            media_id = _first_query_value(query, "v")
        elif len(path_parts) >= 2 and path_parts[0] == "shorts":
            media_id = path_parts[1]
    elif host == "youtu.be" and path_parts:
        media_id = path_parts[0]

    if not media_id or not _is_valid_youtube_id(media_id):
        raise UnsupportedMediaUrlError("unsupported or malformed YouTube media URL")

    clip = ClipInterval(
        start_seconds=_parse_time(_first_query_value(query, "t", "start")),
        end_seconds=_parse_time(_first_query_value(query, "end")),
    )
    canonical_url = f"https://www.youtube.com/watch?v={quote(media_id, safe='-_')}"
    return canonical_url, "youtube", media_id, clip


def _first_query_value(query: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        values = query.get(name)
        if values and values[0]:
            return values[0]
    return None


def _is_valid_youtube_id(media_id: str) -> bool:
    return bool(media_id) and all(character.isascii() and (character.isalnum() or character in "-_") for character in media_id)


def _parse_time(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = _parse_clock_time(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise UnsupportedMediaUrlError("clip times must be finite and non-negative")
    return int(seconds) if seconds.is_integer() else seconds


def _parse_clock_time(value: str) -> float:
    units = {"h": 3600, "m": 60, "s": 1}
    total = 0.0
    number = ""
    for character in value.lower():
        if character.isdigit() or character == ".":
            number += character
            continue
        if character not in units or not number:
            raise UnsupportedMediaUrlError("invalid clip time")
        total += float(number) * units[character]
        number = ""
    if number or not total:
        raise UnsupportedMediaUrlError("invalid clip time")
    return total
