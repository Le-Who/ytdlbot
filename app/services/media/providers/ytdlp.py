"""Heavy yt-dlp adapter; extraction uses the shared cancellable process supervisor.

The race owns its single heavy slot. Transport owns stream downloads, validation,
MP3 conversion, copy muxing, and the one refresh attempt within its own deadline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.services.ytdlp.exceptions import (
    AccessDeniedError,
    ExtractionError,
    LiveStreamError,
    VideoNotFoundError,
)
from app.services.ytdlp.service import YtDlpService

from ..models import (
    MediaCandidate,
    MediaKind,
    MediaRequest,
    MediaSource,
    RefreshDescriptor,
)
from ..registry import FailureKind, ProviderError

Extractor = Callable[[str], Awaitable[dict[str, Any]]]


class YtDlpProvider:
    name = "ytdlp"
    backend_family = "local-ytdlp"
    is_heavy = True

    def __init__(
        self,
        *,
        extract: Extractor | None = None,
        cookie_auth_scopes: frozenset[str] = frozenset(),
    ) -> None:
        self._extract = extract
        self._service = YtDlpService() if extract is None else None
        # Scopes are explicitly authorized by the provider's composition root.
        # Merely supplying a non-public request scope does not authorize cookies.
        self._cookie_auth_scopes = cookie_auth_scopes - {"public"}

    def supports(self, request: MediaRequest) -> bool:
        # yt-dlp's generic extractor also handles embedded media on arbitrary sites.
        return request.kind in (MediaKind.VIDEO, MediaKind.AUDIO) and urlsplit(
            request.canonical_url
        ).scheme in ("http", "https")

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        if not self.supports(request):
            return []
        try:
            if self._service is not None:
                info = await self._service.extract(
                    request.canonical_url,
                    use_cookies=request.auth_scope in self._cookie_auth_scopes,
                )
            else:
                assert self._extract is not None
                info = await self._extract(request.canonical_url)
        except AccessDeniedError as error:
            kind = (
                FailureKind.TRANSIENT
                if request.platform == "youtube"
                else FailureKind.AUTH
            )
            raise ProviderError(kind, str(error)) from error
        except (VideoNotFoundError, LiveStreamError) as error:
            raise ProviderError(FailureKind.PERMANENT, str(error)) from error
        except (ExtractionError, TimeoutError, OSError) as error:
            raise ProviderError(FailureKind.TRANSIENT, str(error)) from error
        if info.get("is_live") or info.get("live_status") == "is_live":
            raise ProviderError(FailureKind.PERMANENT, "active live stream")

        formats = [fmt for fmt in info.get("formats", []) if fmt.get("url")]
        if not formats and info.get("url"):
            formats = [info]
        audio = [fmt for fmt in formats if _codec(fmt, "acodec")]
        if request.audio_language:
            audio = [
                fmt for fmt in audio if fmt.get("language") == request.audio_language
            ]
        audio.sort(
            key=lambda fmt: (fmt.get("vcodec") == "none", _number(fmt.get("abr")) or 0),
            reverse=True,
        )
        candidates: list[MediaCandidate] = []
        if request.kind is MediaKind.AUDIO:
            for fmt in audio:
                candidate = self._candidate(request, info, fmt, None)
                if (
                    request.audio_format
                    and request.audio_format not in candidate.audio_formats
                ):
                    continue
                candidates.append(candidate)
            return candidates

        for video in formats:
            if not _codec(video, "vcodec"):
                continue
            width, height = _integer(video.get("width")), _integer(video.get("height"))
            if (
                request.quality.max_edge
                and min(width or 0, height or 0) != request.quality.max_edge
            ):
                continue
            if _codec(video, "acodec") and (
                not request.audio_language
                or video.get("language") == request.audio_language
            ):
                candidates.append(self._candidate(request, info, video, None))
            elif not _codec(video, "acodec"):
                for sound in audio:
                    if not _codec(sound, "vcodec"):
                        candidates.append(self._candidate(request, info, video, sound))
        candidates.sort(
            key=lambda candidate: min(candidate.width or 0, candidate.height or 0),
            reverse=True,
        )
        return candidates

    def _candidate(
        self,
        request: MediaRequest,
        info: dict[str, Any],
        primary: dict[str, Any],
        audio: dict[str, Any] | None,
    ) -> MediaCandidate:
        parts = (primary, audio) if audio is not None else (primary,)
        sources = tuple(_source(part, info) for part in parts)
        variant = "+".join(source.format_id for source in sources)
        sound = audio if audio is not None else primary
        language = sound.get("language")
        is_audio = request.kind is MediaKind.AUDIO
        mp3 = is_audio and request.audio_format in (None, "mp3")
        size = (
            sum(source.filesize_bytes or 0 for source in sources)
            if all(source.filesize_bytes is not None for source in sources)
            else None
        )
        # A split pair remains a plan, never misrepresented as a muxed direct URL.
        container = (
            "mp3"
            if mp3
            else (
                "mkv"
                if audio is not None and not _mp4_compatible(primary, audio)
                else primary.get("ext")
            )
        )
        return MediaCandidate(
            candidate_id=variant,
            url=request.canonical_url,
            width=None if is_audio else _integer(primary.get("width")),
            height=None if is_audio else _integer(primary.get("height")),
            has_video=not is_audio,
            has_audio=True,
            audio_formats=("mp3",)
            if mp3
            else (str(sound.get("ext") or sound.get("acodec")),),
            audio_languages=(str(language),) if language else (),
            container=container,
            filesize_bytes=None if mp3 else size,
            duration_seconds=_number(info.get("duration")),
            sources=sources,
            complete=True,
            mux_mode="extract-mp3" if mp3 else ("copy" if audio is not None else None),
            refresh=RefreshDescriptor(self.name, request.media_id, variant),
        )

    async def refresh(
        self,
        request: MediaRequest,
        descriptor: RefreshDescriptor,
        *,
        attempt: int,
        deadline: float,
    ) -> MediaCandidate:
        """Re-resolve one variant. Caller must count attempts per materialization."""
        if descriptor.provider != self.name or descriptor.media_id != request.media_id:
            raise ProviderError(FailureKind.PERMANENT, "refresh identity mismatch")
        if attempt != 0 or deadline <= time.monotonic():
            raise ProviderError(FailureKind.TRANSIENT, "refresh budget exhausted")
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                candidates = await self.resolve(request)
        except TimeoutError as error:
            raise ProviderError(
                FailureKind.TRANSIENT, "refresh deadline exceeded"
            ) from error
        for candidate in candidates:
            if candidate.candidate_id == descriptor.variant_id:
                return candidate
        raise ProviderError(FailureKind.PERMANENT, "requested variant disappeared")


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _codec(fmt: dict[str, Any], key: str) -> str | None:
    value = fmt.get(key)
    return str(value) if value and value != "none" else None


def _mp4_compatible(video: dict[str, Any], audio: dict[str, Any]) -> bool:
    return str(video.get("vcodec", "")).startswith(("avc", "h264")) and str(
        audio.get("acodec", "")
    ).startswith(("mp4a", "aac"))


def _source(fmt: dict[str, Any], info: dict[str, Any]) -> MediaSource:
    url = str(fmt["url"])
    expiry = parse_qs(urlsplit(url).query).get("expire", [None])[0]
    headers = {**(info.get("http_headers") or {}), **(fmt.get("http_headers") or {})}
    return MediaSource(
        format_id=str(fmt.get("format_id") or "best"),
        url=url,
        video_codec=_codec(fmt, "vcodec"),
        audio_codec=_codec(fmt, "acodec"),
        container=fmt.get("ext"),
        filesize_bytes=_integer(fmt.get("filesize")),
        expires_at=_number(expiry),
        http_headers=tuple((str(key), str(value)) for key, value in headers.items()),
    )
