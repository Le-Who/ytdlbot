"""One media pipeline shared by Telegram and HTTP entrypoints.

Resolution, materialization, validation, and delivery deliberately have separate
boundaries.  Providers only return candidates; they never send Telegram
messages or choose a user's preferences.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.constants import AUDIO_FORMAT_ID, GIF_FORMAT_ID
from app.core.models import DownloadContext
from app.core.process import run_subprocess

from .delivery import DeliveryAsset, TelegramDelivery
from .models import (
    ClipInterval,
    DeliveryReceipt,
    DeliveryStatus,
    DeliveryTarget,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
    MediaSource,
    QualityPolicy,
    ResolvedMedia,
    UnsupportedMediaUrlError,
)
from .race import RaceConfig, RaceResult, race_candidates
from .registry import ProviderRegistry, ProviderRoute
from .singleflight import SingleFlightGroup
from .transport import MaterializedItem, MediaTransport
from .validation import validate_candidate

ArtifactValidator = Callable[[MediaRequest, MaterializedItem], Awaitable[None]]

if TYPE_CHECKING:
    from app.core.media_cache import MediaCache

_CALLBACK_VERSION = "m2"
_CALLBACK_PART = re.compile(r"^[A-Za-z0-9_.:*+/@=-]+$")
_TRACKING_QUERY_NAMES = frozenset(
    {
        "fbclid",
        "feature",
        "igshid",
        "si",
        "source",
        "s",
        "t",
    }
)
_CONTAINER_FORMATS: dict[str, frozenset[str]] = {
    "m4a": frozenset({"mov", "mp4", "m4a"}),
    "mkv": frozenset({"matroska"}),
    "mp3": frozenset({"mp3"}),
    "mp4": frozenset({"mov", "mp4"}),
    "ogg": frozenset({"ogg"}),
    "opus": frozenset({"ogg"}),
    "wav": frozenset({"wav"}),
    "webm": frozenset({"matroska", "webm"}),
}
_ROUTE_ORDER: dict[str, tuple[str, ...]] = {
    "tiktok": ("tikwm", "ssstik", "cobalt", "gallery-dl", "ytdlp"),
    "twitter": ("fxtwitter", "cobalt", "ytdlp"),
    "x": ("fxtwitter", "cobalt", "ytdlp"),
    "instagram": ("snapsave", "cobalt", "gallery-dl", "ytdlp"),
    "facebook": ("snapsave", "cobalt", "gallery-dl", "ytdlp"),
    "pinterest": ("pinterest", "cobalt", "gallery-dl", "ytdlp"),
    "youtube": ("independent-youtube", "ytdlp"),
}


class DeliveryBackend(Protocol):
    media_cache: MediaCache | None
    bot_id: str | None

    async def deliver(
        self,
        media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset],
        target: DeliveryTarget,
        **kwargs: Any,
    ) -> DeliveryReceipt: ...

    async def retry_failed(
        self,
        receipt: DeliveryReceipt,
        media: MaterializedItem | DeliveryAsset | Sequence[DeliveryAsset],
        target: DeliveryTarget,
        **kwargs: Any,
    ) -> DeliveryReceipt: ...


class Materializer(Protocol):
    async def materialize(
        self,
        request: MediaRequest,
        candidates: Sequence[MediaCandidate],
        *,
        refreshers: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> MaterializedItem: ...


class MediaPipelineError(RuntimeError):
    """Base class for user-visible pipeline failures."""


class MediaResolutionError(MediaPipelineError):
    """No provider produced an equivalent candidate."""


class ArtifactValidationError(MediaPipelineError):
    """The completed file does not satisfy the resolved media contract."""


class CallbackDataError(ValueError):
    """A callback payload is malformed, unsupported, or for another action."""


@dataclass(slots=True)
class _ReadyMaterialization:
    item: MaterializedItem
    users: int


class MediaPipeline:
    """Resolve, materialize, validate, and deliver a canonical media request."""

    def __init__(
        self,
        registry: ProviderRegistry,
        transport: Materializer,
        delivery: DeliveryBackend,
        *,
        media_cache: MediaCache | None = None,
        race_config: RaceConfig | None = None,
        artifact_validator: ArtifactValidator | None = None,
        refreshers: Mapping[str, Any] | None = None,
        enforce_route_matrix: bool = False,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.delivery = delivery
        self.media_cache = media_cache or getattr(delivery, "media_cache", None)
        self.race_config = race_config or RaceConfig()
        self.artifact_validator = artifact_validator or validate_materialized_artifact
        self.refreshers = dict(refreshers or {})
        self.enforce_route_matrix = enforce_route_matrix
        # Domain separation is intentional: the same textual key can never make
        # a resolve factory subscribe to its own materialization flight.
        self._resolve_flights: SingleFlightGroup[str, ResolvedMedia] = (
            SingleFlightGroup()
        )
        self._materialize_flights: SingleFlightGroup[str, MaterializedItem] = (
            SingleFlightGroup()
        )
        self._materialized_lock = asyncio.Lock()
        self._materialized_ready: dict[str, _ReadyMaterialization] = {}
        self._materialized_pending_users: dict[str, int] = {}

    async def resolve(self, request: MediaRequest) -> ResolvedMedia:
        work_request = _public_work_request(request)
        key = f"resolve:{work_request.cache_key}"
        resolved = await self._resolve_flights.do(
            key, lambda: self._resolve_once(work_request)
        )
        if resolved.request is request:
            return resolved
        return replace(resolved, request=request)

    async def deliver(
        self,
        request: MediaRequest,
        target: DeliveryTarget,
        **delivery_options: Any,
    ) -> DeliveryReceipt:
        """Deliver from file-id cache first, then do one shared media pipeline."""
        _validate_delivery_scope(request, target)
        cached_receipt = await self._deliver_cached(request, target, delivery_options)
        if cached_receipt is not None:
            if cached_receipt.success:
                return cached_receipt
            if not cached_receipt.retryable_items:
                return cached_receipt

        resolved = await self.resolve(request)
        async with self.open_materialized(request, resolved=resolved) as materialized:
            materialized.renew_lease()
            if cached_receipt is not None:
                retry = await self.delivery.retry_failed(
                    cached_receipt,
                    materialized,
                    target,
                    request=request,
                    **delivery_options,
                )
                return _merge_receipts(cached_receipt, retry)

            receipt = await self.delivery.deliver(
                materialized,
                target,
                request=request,
                **delivery_options,
            )
            if receipt.retryable_items:
                materialized.renew_lease()
                retried = await self.delivery.retry_failed(
                    receipt,
                    materialized,
                    target,
                    request=request,
                    **delivery_options,
                )
                receipt = _merge_receipts(receipt, retried)
            return receipt

    async def deliver_candidate(
        self,
        request: MediaRequest,
        target: DeliveryTarget,
        candidate: MediaCandidate,
        **delivery_options: Any,
    ) -> DeliveryReceipt:
        """Run an already-authorized direct item through validation and delivery.

        Instagram story/highlight selection already performed the authenticated
        metadata lookup.  This boundary keeps its CDN material isolated by
        ``auth_scope`` while still sharing transport, cache, receipts, and leases.
        """
        _validate_delivery_scope(request, target)
        candidate = _project_album_selection(request, candidate)
        validation = validate_candidate(request, candidate)
        if not validation.usable:
            reasons = ",".join(reason.value for reason in validation.reasons)
            raise MediaResolutionError(
                f"authorized-instagram: candidate rejected ({reasons})"
            )
        cached_receipt = await self._deliver_cached(request, target, delivery_options)
        if cached_receipt is not None:
            if cached_receipt.success:
                return cached_receipt
            if not cached_receipt.retryable_items:
                return cached_receipt
        resolved = ResolvedMedia(
            request=request,
            items=candidate.items or (_candidate_item(request, candidate),),
            candidates=(candidate,),
            provider=candidate.provider,
        )
        async with self.open_materialized(request, resolved=resolved) as materialized:
            materialized.renew_lease()
            if cached_receipt is not None:
                retry = await self.delivery.retry_failed(
                    cached_receipt,
                    materialized,
                    target,
                    request=request,
                    **delivery_options,
                )
                return _merge_receipts(cached_receipt, retry)
            receipt = await self.delivery.deliver(
                materialized,
                target,
                request=request,
                **delivery_options,
            )
            if receipt.retryable_items:
                materialized.renew_lease()
                receipt = _merge_receipts(
                    receipt,
                    await self.delivery.retry_failed(
                        receipt,
                        materialized,
                        target,
                        request=request,
                        **delivery_options,
                    ),
                )
            return receipt

    @asynccontextmanager
    async def open_materialized(
        self,
        request: MediaRequest,
        *,
        resolved: ResolvedMedia | None = None,
    ) -> AsyncIterator[MaterializedItem]:
        """Share completed bytes while retaining the lease for every subscriber."""
        work_request = _public_work_request(request)
        key = f"materialize:{work_request.cache_key}"
        item, registered = await self._register_materialization_user(key)
        try:
            if item is None:

                async def materialize_once() -> MaterializedItem:
                    selected = resolved or await self.resolve(request)
                    candidates = _materialization_candidates(selected.candidates)
                    try:
                        completed = await self.transport.materialize(
                            work_request,
                            candidates,
                            refreshers=self.refreshers,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        provider = selected.provider or "materialization"
                        raise MediaPipelineError(f"{provider}: {error}") from error
                    try:
                        await self.artifact_validator(work_request, completed)
                    except BaseException as error:
                        await _await_preserving_cancellation(
                            completed.release(delete=True)
                        )
                        if isinstance(error, asyncio.CancelledError):
                            raise
                        if isinstance(error, MediaPipelineError):
                            raise
                        provider = selected.provider or "artifact-validation"
                        raise MediaPipelineError(f"{provider}: {error}") from error
                    try:
                        # Publish inside the shared factory.  If its final
                        # subscriber is cancelled after the bytes are ready,
                        # the registered user still owns and releases them.
                        await _await_preserving_cancellation(
                            self._publish_materialization(key, completed)
                        )
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        await _await_preserving_cancellation(
                            completed.release(delete=True)
                        )
                        raise
                    return completed

                item = await self._materialize_flights.do(key, materialize_once)
            item.renew_lease()
            yield item
        finally:
            await _await_preserving_cancellation(
                self._release_materialization_user(key, item, registered=registered)
            )

    async def _resolve_once(self, request: MediaRequest) -> ResolvedMedia:
        routes = self._routes_for(request)
        config = self.race_config
        if request.platform == "youtube":
            config = RaceConfig(
                resolve_timeout=config.resolve_timeout,
                total_timeout=config.total_timeout,
                connect_timeout=config.connect_timeout,
                heavy_delay=0,
            )
        result = await race_candidates(
            request, routes, validate_candidate, config=config
        )
        if result.winner is None:
            raise _resolution_error(routes, result)
        winner = _project_album_selection(request, result.winner.candidate)
        items = winner.items or (_candidate_item(request, winner),)
        resolved = ResolvedMedia(
            request=request,
            items=items,
            candidates=(winner,),
            provider=result.winner.provider,
        )
        await self._store_metadata(resolved)
        return resolved

    def _routes_for(self, request: MediaRequest) -> tuple[ProviderRoute, ...]:
        routes = self.registry.routes_for(request)
        if request.auth_scope != "public":
            routes = tuple(
                route
                for route in routes
                if route.provider.name in {"authorized-instagram", "ytdlp"}
            )
        if not self.enforce_route_matrix:
            return routes
        order = _ROUTE_ORDER.get(request.platform, ("ytdlp",))
        positions = {name: index for index, name in enumerate(order)}
        return tuple(
            sorted(
                (route for route in routes if route.provider.name in positions),
                key=lambda route: positions[route.provider.name],
            )
        )

    async def _deliver_cached(
        self,
        request: MediaRequest,
        target: DeliveryTarget,
        options: Mapping[str, Any],
    ) -> DeliveryReceipt | None:
        cache = self.media_cache
        bot_id = getattr(self.delivery, "bot_id", None)
        if cache is None or not bot_id:
            return None
        try:
            metadata = await cache.get_metadata(request)
            if request.album_selection:
                item_count = len(request.album_selection)
            elif metadata is not None:
                item_count = metadata.item_count
            elif request.kind in {
                MediaKind.VIDEO,
                MediaKind.AUDIO,
                MediaKind.PHOTO,
                MediaKind.ANIMATION,
                MediaKind.DOCUMENT,
            }:
                item_count = 1
            else:
                # AUTO and ALBUM can represent multiple outputs.  Without
                # stable cardinality metadata, item zero is not proof that the
                # complete ordered result is cached.
                return None
            indexes = tuple(range(item_count))
            assets: list[DeliveryAsset] = []
            for index in indexes:
                cached = await cache.get_delivery(
                    request, bot_id=bot_id, item_index=index
                )
                if cached is None:
                    return None
                try:
                    kind = MediaKind(str(cached.telegram_type))
                except ValueError:
                    return None
                assets.append(
                    DeliveryAsset(
                        MediaItem(
                            f"{request.media_id}:{index}",
                            kind,
                            request.canonical_url,
                        ),
                        None,
                        item_index=index,
                    )
                )
        except Exception:  # noqa: BLE001 - cache acceleration is non-authoritative
            return None
        return await self.delivery.deliver(
            tuple(assets), target, request=request, **dict(options)
        )

    async def _store_metadata(self, resolved: ResolvedMedia) -> None:
        if self.media_cache is None:
            return
        from app.core.media_cache import StableMediaMetadata

        candidate = resolved.candidates[0]
        first = resolved.items[0]
        metadata = StableMediaMetadata(
            platform=resolved.request.platform,
            media_id=resolved.request.media_id,
            kind=candidate.kind or first.kind,
            title=first.title,
            duration_seconds=candidate.duration_seconds,
            width=candidate.width,
            height=candidate.height,
            item_count=len(resolved.items),
            album_order=tuple(range(len(resolved.items))),
            provider=resolved.provider,
        )
        try:
            await self.media_cache.put_metadata(resolved.request, metadata)
        except Exception:  # noqa: BLE001 - metadata cache is best effort
            return

    async def _register_materialization_user(
        self, key: str
    ) -> tuple[MaterializedItem | None, bool]:
        async with self._materialized_lock:
            ready = self._materialized_ready.get(key)
            if ready is not None:
                ready.users += 1
                return ready.item, True
            self._materialized_pending_users[key] = (
                self._materialized_pending_users.get(key, 0) + 1
            )
            return None, True

    async def _publish_materialization(self, key: str, item: MaterializedItem) -> None:
        async with self._materialized_lock:
            if key not in self._materialized_ready:
                users = self._materialized_pending_users.pop(key, 0)
                self._materialized_ready[key] = _ReadyMaterialization(item, users)

    async def _release_materialization_user(
        self,
        key: str,
        item: MaterializedItem | None,
        *,
        registered: bool,
    ) -> None:
        if not registered:
            return
        release: MaterializedItem | None = None
        async with self._materialized_lock:
            ready = self._materialized_ready.get(key)
            if ready is None:
                pending = self._materialized_pending_users.get(key, 0) - 1
                if pending > 0:
                    self._materialized_pending_users[key] = pending
                else:
                    self._materialized_pending_users.pop(key, None)
            else:
                ready.users -= 1
                if ready.users <= 0:
                    self._materialized_ready.pop(key, None)
                    release = ready.item
        if release is not None:
            await release.release(delete=True)


def build_media_request(
    url: str,
    *,
    kind: MediaKind | str = MediaKind.AUTO,
    quality: int | QualityPolicy | None = None,
    audio_format: str | None = None,
    audio_language: str | None = None,
    clip: ClipInterval | str | None = None,
    album_selection: Sequence[int] = (),
    watermark_allowed: bool = True,
    caller_scope: str = "public",
    auth_scope: str = "public",
    exact: bool = True,
    deadline: float | None = None,
) -> MediaRequest:
    """Build the same immutable request for every bot/API entrypoint."""
    media_kind = kind if isinstance(kind, MediaKind) else MediaKind(kind)
    quality_policy = (
        quality
        if isinstance(quality, QualityPolicy)
        else QualityPolicy(max_edge=quality)
    )
    interval = _parse_clip(clip)
    try:
        return MediaRequest.from_url(
            url,
            kind=media_kind,
            quality=quality_policy,
            audio_format=audio_format,
            audio_language=audio_language,
            clip=interval,
            album_selection=tuple(album_selection),
            watermark_allowed=watermark_allowed,
            caller_scope=caller_scope,
            auth_scope=auth_scope,
            exact=exact,
            deadline=deadline,
        )
    except UnsupportedMediaUrlError:
        canonical, platform, media_id = _canonicalize_generic_url(url)
        return MediaRequest(
            canonical_url=canonical,
            platform=platform,
            media_id=media_id,
            kind=media_kind,
            quality=quality_policy,
            audio_format=audio_format,
            audio_language=audio_language,
            clip=interval or ClipInterval(),
            album_selection=tuple(album_selection),
            watermark_allowed=watermark_allowed,
            caller_scope=caller_scope,
            auth_scope=auth_scope,
            exact=exact,
            deadline=deadline,
        )


def request_from_download_context(
    payload: DownloadContext,
    *,
    caller_scope: str,
    auth_scope: str = "public",
    exact: bool = True,
) -> MediaRequest:
    """Translate legacy picker state without interpreting provider format IDs."""
    if payload.format_id == AUDIO_FORMAT_ID:
        kind = MediaKind.AUDIO
        audio_format = "mp3"
    elif payload.format_id == GIF_FORMAT_ID:
        kind = MediaKind.ANIMATION
        audio_format = None
    else:
        kind = MediaKind.VIDEO
        audio_format = None
    return build_media_request(
        payload.page_url,
        kind=kind,
        quality=payload.height,
        audio_format=audio_format,
        clip=payload.section,
        caller_scope=caller_scope,
        auth_scope=auth_scope,
        exact=exact,
    )


def encode_callback_data(action: str, *parts: str) -> str:
    values = (_CALLBACK_VERSION, action, *parts)
    _validate_callback_parts(values, require_payload=True)
    encoded = "|".join(values)
    if len(encoded.encode("utf-8")) > 64:
        raise CallbackDataError("callback data exceeds Telegram's 64-byte limit")
    return encoded


def decode_callback_data(
    payload: str,
    *,
    expected_action: str,
    max_parts: int = 3,
) -> tuple[str, ...]:
    """Accept m2 plus the previous unversioned shape for its cache TTL."""
    action, parts = decode_callback_payload(
        payload, allowed_actions=(expected_action,), max_parts=max_parts
    )
    assert action == expected_action
    return parts


def decode_callback_payload(
    payload: str,
    *,
    allowed_actions: Sequence[str],
    max_parts: int = 3,
) -> tuple[str, tuple[str, ...]]:
    """Decode one callback while constraining action and shape at the boundary."""
    if not payload or len(payload.encode("utf-8")) > 64:
        raise CallbackDataError("invalid callback data")
    values = tuple(payload.split("|"))
    if values[:1] == (_CALLBACK_VERSION,):
        if len(values) < 3:
            raise CallbackDataError("versioned callback data is incomplete")
        action, parts = values[1], values[2:]
    else:
        if len(values) < 2:
            raise CallbackDataError("legacy callback data is incomplete")
        action, parts = values[0], values[1:]
    if action not in allowed_actions or len(parts) > max_parts:
        raise CallbackDataError("callback action or shape is invalid")
    _validate_callback_parts((action, *parts), require_payload=True)
    return action, parts


async def validate_materialized_artifact(
    request: MediaRequest, media: MaterializedItem
) -> None:
    """Use ffprobe to confirm each final artifact's required streams and quality."""
    candidate = media.candidate
    expected = candidate.items or (_candidate_item(request, candidate),)
    if len(expected) != len(media.paths):
        raise ArtifactValidationError("materialized item order/count changed")
    for item_index, (path, item) in enumerate(zip(media.paths, expected, strict=True)):
        probe = await _ffprobe(path)
        streams = probe.get("streams")
        if not isinstance(streams, list):
            raise ArtifactValidationError("ffprobe returned no stream list")
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if (
            item.kind in {MediaKind.VIDEO, MediaKind.ANIMATION, MediaKind.PHOTO}
            and not video
        ):
            raise ArtifactValidationError("final artifact has no visual stream")
        if item.kind is MediaKind.AUDIO and not audio:
            raise ArtifactValidationError("final artifact has no audio stream")
        if item.kind is MediaKind.VIDEO and candidate.has_audio and not audio:
            raise ArtifactValidationError("final video has no required audio stream")
        if request.quality.max_edge is not None and item.kind is MediaKind.VIDEO:
            stream = video[0]
            try:
                short_edge = min(int(stream["width"]), int(stream["height"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ArtifactValidationError(
                    "final video dimensions are unavailable"
                ) from error
            if short_edge < request.quality.max_edge:
                raise ArtifactValidationError("final video is below requested quality")
        format_data = probe.get("format")
        if not isinstance(format_data, dict):
            raise ArtifactValidationError("ffprobe returned no format metadata")
        format_names = {
            value.strip().lower()
            for value in str(format_data.get("format_name") or "").split(",")
            if value.strip()
        }
        expected_container = (item.container or candidate.container or "").lower()
        accepted_formats = _CONTAINER_FORMATS.get(expected_container)
        if accepted_formats is not None and not format_names.intersection(
            accepted_formats
        ):
            raise ArtifactValidationError("final artifact container does not match")

        audio_codecs = {
            _normalize_codec(str(stream.get("codec_name") or "")) for stream in audio
        }
        if (
            request.kind is MediaKind.AUDIO
            and request.audio_format == "mp3"
            and ("mp3" not in format_names or "mp3" not in audio_codecs)
        ):
            raise ArtifactValidationError("final artifact is not MP3 audio")
        if item.kind is MediaKind.ANIMATION:
            if audio:
                raise ArtifactValidationError("final animation must not contain audio")
            duration = _probe_duration(format_data)
            if duration is None or duration > 60.5:
                raise ArtifactValidationError(
                    "final animation exceeds the 60 second limit"
                )

        _validate_stream_codecs(
            request,
            candidate,
            video,
            audio,
            item_index=item_index,
            output_container=expected_container,
        )
        expected_duration = _expected_clip_duration(request, candidate)
        if expected_duration is not None:
            actual_duration = _probe_duration(format_data)
            tolerance = max(1.5, expected_duration * 0.05)
            if (
                actual_duration is None
                or abs(actual_duration - expected_duration) > tolerance
            ):
                raise ArtifactValidationError("final clip duration does not match")


def _validate_stream_codecs(
    request: MediaRequest,
    candidate: MediaCandidate,
    video: Sequence[Mapping[str, Any]],
    audio: Sequence[Mapping[str, Any]],
    *,
    item_index: int,
    output_container: str,
) -> None:
    if candidate.mux_mode == "extract-mp3":
        return
    clip_requested = (
        request.clip.start_seconds is not None or request.clip.end_seconds is not None
    )
    if clip_requested and candidate.mux_mode != "mute-mp4":
        expected_video, expected_audio = _transcoded_codecs(candidate, output_container)
    else:
        if candidate.items and len(candidate.sources) == len(candidate.items):
            sources = candidate.sources[item_index : item_index + 1]
        else:
            sources = candidate.sources
        expected_video = {
            _normalize_codec(source.video_codec)
            for source in sources
            if source.video_codec and source.video_codec != "none"
        }
        expected_audio = {
            _normalize_codec(source.audio_codec)
            for source in sources
            if source.audio_codec and source.audio_codec != "none"
        }
    actual_video = {
        _normalize_codec(str(stream.get("codec_name") or "")) for stream in video
    }
    actual_audio = {
        _normalize_codec(str(stream.get("codec_name") or "")) for stream in audio
    }
    if expected_video and not expected_video.intersection(actual_video):
        raise ArtifactValidationError("final video codec does not match")
    if expected_audio and not expected_audio.intersection(actual_audio):
        raise ArtifactValidationError("final audio codec does not match")


def _transcoded_codecs(
    candidate: MediaCandidate, output_container: str
) -> tuple[set[str], set[str]]:
    if not candidate.has_video:
        audio_codec = {
            "flac": "flac",
            "ogg": "opus",
            "opus": "opus",
            "wav": "pcm_s16le",
        }.get(output_container, "aac")
        return set(), {audio_codec}
    if output_container == "webm":
        return {"vp9"}, {"opus"} if candidate.has_audio else set()
    if output_container == "gif":
        return {"gif"}, set()
    return {"h264"}, {"aac"} if candidate.has_audio else set()


def _normalize_codec(value: str) -> str:
    codec = value.strip().lower().split(".", 1)[0]
    aliases = {
        "av01": "av1",
        "avc1": "h264",
        "hev1": "hevc",
        "hvc1": "hevc",
        "mp4a": "aac",
        "vp09": "vp9",
    }
    return aliases.get(codec, codec)


def _probe_duration(format_data: Mapping[str, Any]) -> float | None:
    try:
        duration = float(format_data["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def _expected_clip_duration(
    request: MediaRequest, candidate: MediaCandidate
) -> float | None:
    start = request.clip.start_seconds
    end = request.clip.end_seconds
    if start is None and end is None:
        return None
    clip_start = start or 0
    if end is not None:
        return max(0, end - clip_start)
    if candidate.duration_seconds is not None:
        return max(0, candidate.duration_seconds - clip_start)
    return None


async def _ffprobe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        async with run_subprocess(command, timeout=20) as handle:
            if handle.proc.stdout is None:
                raise ArtifactValidationError("ffprobe stdout is unavailable")
            stdout = await handle.proc.stdout.read()
            return_code = await handle.wait()
    except (OSError, TimeoutError) as error:
        raise ArtifactValidationError("ffprobe could not validate media") from error
    if return_code != 0:
        raise ArtifactValidationError("ffprobe rejected final media")
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactValidationError("ffprobe returned invalid metadata") from error
    if not isinstance(value, dict):
        raise ArtifactValidationError("ffprobe returned invalid metadata")
    return value


def build_default_pipeline(bot: Any) -> MediaPipeline:
    """Compose only explicitly configured/verified external providers."""
    from app.core import config, state
    from app.core.media_cache import MediaCache
    from app.services.media.providers.cobalt import CobaltProvider
    from app.services.media.providers.fxtwitter import FxTwitterProvider
    from app.services.media.providers.gallerydl import GalleryDlProvider
    from app.services.media.providers.http import ProviderEndpoint
    from app.services.media.providers.snapsave import SnapSaveProvider
    from app.services.media.providers.ssstik import SSSTikProvider
    from app.services.media.providers.tikwm import TikWMProvider
    from app.services.media.providers.ytdlp import YtDlpProvider
    from app.services.pinterest import PinterestProvider

    routes: list[ProviderRoute] = [
        ProviderRoute(TikWMProvider()),
        ProviderRoute(SSSTikProvider()),
        ProviderRoute(FxTwitterProvider()),
        ProviderRoute(PinterestProvider()),
    ]
    snapsave_verified = bool(getattr(config, "SNAPSAVE_CONTRACT_VERIFIED", False))
    routes.append(
        ProviderRoute(
            SnapSaveProvider(contract_verified=snapsave_verified),
            enabled=snapsave_verified,
            capability_available=snapsave_verified,
        )
    )
    cobalt_verified = bool(getattr(config, "COBALT_CONTRACT_VERIFIED", False))
    cobalt_capabilities = frozenset(
        getattr(
            config,
            "COBALT_CAPABILITIES",
            {"tiktok", "twitter", "x", "instagram", "facebook", "pinterest"},
        )
    )
    endpoints = tuple(
        ProviderEndpoint(
            origin,
            getattr(config, "COBALT_API_KEY", None) or None,
            cobalt_capabilities,
            cobalt_verified,
        )
        for origin in getattr(config, "COBALT_API_URLS", ())
    )
    if endpoints:
        routes.append(
            ProviderRoute(
                CobaltProvider(endpoints),
                enabled=cobalt_verified,
                capability_available=cobalt_verified,
            )
        )
    gallery = GalleryDlProvider()
    routes.append(
        ProviderRoute(
            gallery,
            enabled=gallery.available,
            capability_available=gallery.available,
        )
    )
    ytdlp = YtDlpProvider()
    routes.append(ProviderRoute(ytdlp))
    cache = MediaCache(state.redis_client)
    bot_id = str(getattr(bot, "id", "")) or None
    delivery = TelegramDelivery(bot, media_cache=cache, bot_id=bot_id)
    return MediaPipeline(
        ProviderRegistry(routes, revision=_provider_revision(routes)),
        MediaTransport(),
        cast(DeliveryBackend, delivery),
        media_cache=cache,
        refreshers={ytdlp.name: ytdlp},
        enforce_route_matrix=True,
    )


def _provider_revision(routes: Sequence[ProviderRoute]) -> str:
    state = tuple(
        (route.provider.name, route.enabled, route.capability_available)
        for route in routes
    )
    return hashlib.sha256(repr(state).encode()).hexdigest()


def _resolution_error(
    routes: Sequence[ProviderRoute], result: RaceResult
) -> MediaResolutionError:
    if result.failures:
        details = "; ".join(
            f"{failure.provider}: {failure.message}" for failure in result.failures
        )
    elif result.rejections:
        details = "; ".join(
            f"{rejection.provider}: "
            + ",".join(reason.value for reason in rejection.validation.reasons)
            for rejection in result.rejections
        )
    elif routes:
        details = ", ".join(route.provider.name for route in routes)
        details = f"providers returned no usable candidates ({details})"
    else:
        details = "no healthy provider supports this request"
    return MediaResolutionError(details)


def _candidate_item(request: MediaRequest, candidate: MediaCandidate) -> MediaItem:
    kind = candidate.kind
    if kind in {None, MediaKind.AUTO, MediaKind.ALBUM}:
        kind = MediaKind.AUDIO if request.kind is MediaKind.AUDIO else MediaKind.VIDEO
    return MediaItem(
        media_id=request.media_id,
        kind=kind,
        url=candidate.url,
        duration_seconds=candidate.duration_seconds,
        width=candidate.width,
        height=candidate.height,
        container=candidate.container,
        filesize_bytes=candidate.filesize_bytes,
    )


def _public_work_request(request: MediaRequest) -> MediaRequest:
    if request.auth_scope == "public" and request.caller_scope != "public":
        return replace(request, caller_scope="public")
    return request


def _project_album_selection(
    request: MediaRequest, candidate: MediaCandidate
) -> MediaCandidate:
    selection = request.album_selection
    if not selection:
        return candidate
    if not candidate.items:
        if selection == (0,):
            return candidate
        raise MediaResolutionError(
            f"{candidate.provider or 'provider'}: album selection is unavailable"
        )
    try:
        items = tuple(candidate.items[index] for index in selection)
    except IndexError as error:
        raise MediaResolutionError(
            f"{candidate.provider or 'provider'}: album selection is out of range"
        ) from error
    sources_by_url = {source.url: source for source in candidate.sources}
    sources = tuple(
        replace(source, format_id=str(output_index))
        if (source := sources_by_url.get(item.url)) is not None
        else MediaSource(
            format_id=str(output_index),
            url=item.url,
            container=item.container,
            filesize_bytes=item.filesize_bytes,
        )
        for output_index, item in enumerate(items)
    )
    return replace(candidate, items=items, sources=sources)


def _validate_delivery_scope(request: MediaRequest, target: DeliveryTarget) -> None:
    if request.auth_scope != target.auth_scope:
        raise MediaResolutionError("delivery auth scope does not match request")


def _materialization_candidates(
    candidates: Sequence[MediaCandidate],
) -> tuple[MediaCandidate, ...]:
    if not candidates:
        raise MediaResolutionError("resolved media contains no candidates")
    normalized: list[MediaCandidate] = []
    for candidate in candidates:
        if not candidate.items:
            normalized.append(candidate)
            continue
        # Album items are the ordered delivery contract.  Provider payloads may
        # also expose a slideshow soundtrack; it must not silently become an
        # extra Telegram item or trigger a default slideshow conversion.
        sources_by_url = {source.url: source for source in candidate.sources}
        sources = tuple(
            replace(source, format_id=str(index))
            if (source := sources_by_url.get(item.url)) is not None
            else MediaSource(
                format_id=str(index),
                url=item.url,
                container=item.container,
                filesize_bytes=item.filesize_bytes,
            )
            for index, item in enumerate(candidate.items)
        )
        normalized.append(replace(candidate, sources=sources))
    return tuple(normalized)


def _merge_receipts(
    original: DeliveryReceipt, retried: DeliveryReceipt
) -> DeliveryReceipt:
    replacements = {item.item_index: item for item in retried.items}
    items = tuple(replacements.get(item.item_index, item) for item in original.items)
    statuses = {item.status for item in items}
    if items and all(item.success for item in items):
        status = DeliveryStatus.SUCCESS
    elif statuses == {DeliveryStatus.UNCERTAIN}:
        status = DeliveryStatus.UNCERTAIN
    elif statuses == {DeliveryStatus.FAILED}:
        status = DeliveryStatus.FAILED
    else:
        status = DeliveryStatus.PARTIAL
    return DeliveryReceipt(original.target, items, status)


async def _await_preserving_cancellation(operation: Awaitable[Any]) -> Any:
    """Finish ownership cleanup/publication, then restore caller cancellation."""
    task: asyncio.Future[Any] = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


def _parse_clip(value: ClipInterval | str | None) -> ClipInterval | None:
    if value is None or isinstance(value, ClipInterval):
        return value
    raw = value.strip().removeprefix("*")
    if not raw:
        return ClipInterval()
    if "-" not in raw:
        raise ValueError("clip must contain a start-end interval")
    start, end = raw.split("-", 1)
    return ClipInterval(_clock_seconds(start), _clock_seconds(end))


def _clock_seconds(value: str) -> float:
    stripped = value.strip()
    if ":" not in stripped:
        seconds = float(stripped)
    else:
        parts = [float(part) for part in stripped.split(":")]
        if len(parts) not in {2, 3}:
            raise ValueError("invalid clip clock")
        seconds = sum(part * (60**index) for index, part in enumerate(reversed(parts)))
    if seconds < 0:
        raise ValueError("clip seconds must not be negative")
    return int(seconds) if seconds.is_integer() else seconds


def _canonicalize_generic_url(url: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as error:
        raise UnsupportedMediaUrlError("malformed media URL") from error
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise UnsupportedMediaUrlError("media URL must use HTTP(S) and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise UnsupportedMediaUrlError("media URL must not contain credentials")
    platform = _platform_for_host(host)
    netloc = host if port is None else f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in _TRACKING_QUERY_NAMES
    ]
    canonical = urlunsplit(("https", netloc, parsed.path or "/", urlencode(query), ""))
    media_id = _generic_media_id(platform, parsed.path, query, canonical)
    return canonical, platform, media_id


def _platform_for_host(host: str) -> str:
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host in {"x.com", "twitter.com"} or host.endswith((".x.com", ".twitter.com")):
        return "x"
    if host in {"instagram.com", "instagr.am"} or host.endswith(
        (".instagram.com", ".instagr.am")
    ):
        return "instagram"
    if host in {"facebook.com", "fb.watch"} or host.endswith(".facebook.com"):
        return "facebook"
    if host in {"pinterest.com", "pin.it"} or host.endswith(".pinterest.com"):
        return "pinterest"
    if host in {"vk.com", "vkvideo.ru"} or host.endswith((".vk.com", ".vkvideo.ru")):
        return "vk"
    if host == "rutube.ru" or host.endswith(".rutube.ru"):
        return "rutube"
    return "other"


def _generic_media_id(
    platform: str,
    path: str,
    query: Sequence[tuple[str, str]],
    canonical: str,
) -> str:
    parts = [part for part in path.split("/") if part]
    if platform == "x" and "status" in parts:
        position = parts.index("status")
        if position + 1 < len(parts):
            return parts[position + 1]
    if platform == "tiktok":
        for marker in ("video", "photo"):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                return parts[parts.index(marker) + 1]
    if (
        platform == "instagram"
        and len(parts) >= 2
        and parts[0]
        in {
            "p",
            "reel",
            "tv",
            "stories",
        }
    ):
        return parts[-1]
    if platform == "pinterest" and "pin" in parts:
        position = parts.index("pin")
        if position + 1 < len(parts):
            return parts[position + 1]
    query_map = dict(query)
    for name in ("v", "id", "story_fbid"):
        if query_map.get(name):
            return query_map[name]
    if parts:
        return parts[-1]
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _validate_callback_parts(values: Sequence[str], *, require_payload: bool) -> None:
    if require_payload and len(values) < 2:
        raise CallbackDataError("callback data is incomplete")
    if any(not value or not _CALLBACK_PART.fullmatch(value) for value in values):
        raise CallbackDataError("callback data contains invalid characters")


__all__ = [
    "ArtifactValidationError",
    "CallbackDataError",
    "MediaPipeline",
    "MediaPipelineError",
    "MediaResolutionError",
    "build_default_pipeline",
    "build_media_request",
    "decode_callback_data",
    "decode_callback_payload",
    "encode_callback_data",
    "request_from_download_context",
    "validate_materialized_artifact",
]
