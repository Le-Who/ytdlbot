from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.media_cache import CachedDelivery, MediaCache
from app.core.metrics import MetricsCollector
from app.services.media import pipeline as pipeline_module
from app.services.media.models import (
    ClipInterval,
    DeliveredItem,
    DeliveryReceipt,
    DeliveryStatus,
    DeliveryTarget,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
    MediaSource,
)
from app.services.media.pipeline import (
    ArtifactValidationError,
    MediaPipeline,
    MediaPipelineError,
    MediaResolutionError,
    build_default_pipeline,
    build_media_request,
    validate_materialized_artifact,
)
from app.services.media.race import RaceConfig
from app.services.media.registry import ProviderRegistry, ProviderRoute
from app.services.media.transport import MaterializationError, MaterializedItem


class _Reservation:
    def __init__(self) -> None:
        self.renewed = 0
        self.released = 0

    def renew(self) -> None:
        self.renewed += 1

    async def release(self) -> None:
        self.released += 1


@dataclass
class _Provider:
    name: str
    candidate: MediaCandidate | None = None
    delay: float = 0
    is_heavy: bool = False
    backend_family: str = "test"
    cancelled: bool = False
    calls: int = 0

    def supports(self, request: MediaRequest) -> bool:
        return True

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [self.candidate] if self.candidate is not None else []


class _Transport:
    def __init__(self, item: MaterializedItem) -> None:
        self.item = item
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.seen_candidates = ()

    async def materialize(self, request, candidates, **kwargs):
        self.calls += 1
        self.seen_candidates = tuple(candidates)
        self.started.set()
        if self.release.is_set():
            return self.item
        await self.release.wait()
        return self.item


class _Delivery:
    def __init__(self, cache: MediaCache | None = None) -> None:
        self.media_cache = cache
        self.bot_id = "bot-1"
        self.calls: list[object] = []
        self.retry_calls = 0
        self.receipts: list[DeliveryReceipt] = []

    async def deliver(self, media, target, **kwargs):
        self.calls.append(media)
        if self.receipts:
            return self.receipts.pop(0)
        if isinstance(media, tuple):
            assets = media
        elif hasattr(media, "paths"):
            items = media.candidate.items or (
                MediaItem("abc123", MediaKind.VIDEO, media.candidate.url),
            )
            assets = tuple(SimpleNamespace(item=item) for item in items)
        else:
            assets = (media,)
        delivered = tuple(
            DeliveredItem(
                item=asset.item,
                status=DeliveryStatus.SUCCESS,
                telegram_type=asset.item.kind,
                item_index=index,
                file_id=f"file-{index}",
            )
            for index, asset in enumerate(assets)
        )
        return DeliveryReceipt(target, delivered, DeliveryStatus.SUCCESS)

    async def retry_failed(self, receipt, media, target, **kwargs):
        self.retry_calls += 1
        return await self.deliver(media, target, **kwargs)


def _request(*, kind: MediaKind = MediaKind.VIDEO) -> MediaRequest:
    return MediaRequest.from_url("https://youtu.be/abc123", kind=kind, exact=True)


def _candidate(
    provider: str = "winner", *, items: tuple[MediaItem, ...] = ()
) -> MediaCandidate:
    return MediaCandidate(
        candidate_id="720",
        url="https://cdn.example/video.mp4",
        width=1280,
        height=720,
        has_video=True,
        has_audio=True,
        container="mp4",
        provider=provider,
        backend_family=f"{provider}-family",
        media_id="abc123",
        kind=MediaKind.ALBUM if items else MediaKind.VIDEO,
        items=items,
    )


def _materialized(tmp_path: Path, candidate: MediaCandidate):
    paths = []
    count = len(candidate.items) or 1
    for index in range(count):
        path = tmp_path / f"item-{index}.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypisom")
        paths.append(path)
    reservation = _Reservation()
    return MaterializedItem(
        tuple(paths), 12 * count, candidate, reservation
    ), reservation


@pytest.mark.asyncio
async def test_cached_file_id_is_delivered_before_any_provider_or_transport_work(
    tmp_path: Path,
):
    request = _request()
    cache = MediaCache()
    await cache.put_delivery(
        request,
        bot_id="bot-1",
        delivery=CachedDelivery("cached-file", MediaKind.VIDEO, 0),
    )
    provider = _Provider("never", _candidate("never"))
    media, reservation = _materialized(tmp_path, provider.candidate)
    transport = _Transport(media)
    delivery = _Delivery(cache)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("123"))

    assert receipt.success
    assert provider.calls == 0
    assert transport.calls == 0
    assert reservation.released == 0


@pytest.mark.asyncio
async def test_cache_miss_races_resolvers_then_delivers_one_valid_winner(
    tmp_path: Path,
):
    request = _request()
    winner = _Provider("independent", _candidate("independent"), delay=0.01)
    loser = _Provider("hung", _candidate("hung"), delay=60)
    materialized, reservation = _materialized(tmp_path, winner.candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = _Delivery()
    validate_artifact = AsyncMock()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(loser), ProviderRoute(winner)]),
        transport,
        delivery,
        artifact_validator=validate_artifact,
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("123"))

    assert receipt.success
    assert winner.calls == 1
    assert loser.cancelled
    validate_artifact.assert_awaited_once_with(request, materialized)
    assert reservation.renewed >= 1
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_slideshow_video_uses_soundtrack_validation_delivery_and_lease_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    collector = MetricsCollector()
    monkeypatch.setattr(pipeline_module, "metrics", collector)
    request = build_media_request(
        "https://www.tiktok.com/@tester/photo/123",
        clip="10-20",
        caller_scope="group",
        exact=False,
    )
    image_one = tmp_path / "one.jpg"
    image_two = tmp_path / "two.jpg"
    soundtrack = tmp_path / "soundtrack.mp3"
    video = tmp_path / "slideshow.mp4"
    for path in (image_one, image_two, soundtrack):
        path.write_bytes(b"source")
    items = (
        MediaItem("123:0", MediaKind.PHOTO, "https://cdn.example/one.jpg"),
        MediaItem("123:1", MediaKind.PHOTO, "https://cdn.example/two.jpg"),
    )
    candidate = MediaCandidate(
        candidate_id="album",
        url=items[0].url,
        has_video=False,
        has_audio=True,
        sources=(
            MediaSource("0", items[0].url, container="image"),
            MediaSource("1", items[1].url, container="image"),
            MediaSource(
                "audio",
                "https://cdn.example/music.mp3",
                audio_codec="unknown",
                container="mp3",
            ),
        ),
        provider="tikwm",
        media_id="123",
        kind=MediaKind.ALBUM,
        items=items,
    )
    reservations: list[_Reservation] = []

    class DerivedReservation(_Reservation):
        def __init__(self) -> None:
            super().__init__()
            self.release_started = False

        def renew(self) -> None:
            if self.release_started:
                raise RuntimeError("heartbeat raced released derived artifact")
            super().renew()

        async def release(self) -> None:
            self.release_started = True
            await asyncio.sleep(0.025)
            await super().release()

    class Transport:
        async def materialize(self, materialize_request, candidates, **kwargs):
            selected = candidates[0]
            reservation = _Reservation()
            reservations.append(reservation)
            paths = (
                (soundtrack,)
                if materialize_request.kind is MediaKind.AUDIO
                else (image_one, image_two)
            )
            return MaterializedItem(paths, 6 * len(paths), selected, reservation)

        async def adopt_local(self, adopt_request, path, selected, *, deadline=None):
            del deadline
            assert adopt_request.clip == ClipInterval(10, 20)
            reservation = DerivedReservation()
            reservations.append(reservation)
            return MaterializedItem((path,), path.stat().st_size, selected, reservation)

    converter_calls = []

    async def convert(images, audio_path):
        converter_calls.append((images, audio_path))
        video.write_bytes(b"derived-video")
        return str(video)

    class Delivery(_Delivery):
        async def deliver(self, media, target, **kwargs):
            await asyncio.sleep(0.035)
            return await super().deliver(media, target, **kwargs)

    validator_calls = []

    async def validate(validation_request, media):
        validator_calls.append((validation_request, media))

    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("tikwm", candidate))]),
        Transport(),
        Delivery(),
        artifact_validator=validate,
        slideshow_converter=convert,
        lease_renew_interval=0.01,
    )

    receipt = await pipeline.deliver_slideshow_video(
        request,
        DeliveryTarget("9", caller_scope="group"),
        caption="👤 @tester",
    )

    assert receipt.success
    assert converter_calls == [([str(image_one), str(image_two)], str(soundtrack))]
    assert [call[0].kind for call in validator_calls] == [
        MediaKind.AUTO,
        MediaKind.AUDIO,
        MediaKind.VIDEO,
    ]
    derived = validator_calls[-1][1]
    derived_request = validator_calls[-1][0]
    normal_video_request = replace(
        request,
        kind=MediaKind.VIDEO,
        album_selection=(),
        exact=True,
    )
    assert derived_request.clip == ClipInterval(10, 20)
    assert derived_request.output_variant == "slideshow-video"
    assert derived_request.cache_key != normal_video_request.cache_key
    assert derived.candidate.kind is MediaKind.VIDEO
    assert derived.candidate.has_audio
    assert all(reservation.renewed >= 3 for reservation in reservations[:2])
    assert reservations[-1].renewed >= 2
    assert all(reservation.released == 1 for reservation in reservations)
    assert not video.exists()
    assert collector.pipeline_results.collect() == [
        ({"platform": "tiktok", "status": "success"}, 1.0)
    ]
    total = [
        count
        for labels, count, _ in collector.pipeline_duration.collect()
        if labels.get("phase") == "total"
    ]
    assert total == [1]


@pytest.mark.asyncio
async def test_resolve_and_materialize_use_separate_singleflight_domains(
    tmp_path: Path,
):
    request = _request()
    provider = _Provider("winner", _candidate())
    materialized, reservation = _materialized(tmp_path, provider.candidate)
    transport = _Transport(materialized)
    delivery = _Delivery()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    first = asyncio.create_task(pipeline.deliver(request, DeliveryTarget("1")))
    second = asyncio.create_task(pipeline.deliver(request, DeliveryTarget("2")))
    await transport.started.wait()
    transport.release.set()
    receipts = await asyncio.gather(first, second)

    assert all(receipt.success for receipt in receipts)
    assert provider.calls == 1
    assert transport.calls == 1
    assert reservation.renewed >= 2
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_late_materialization_subscriber_does_not_repeat_resolution(
    tmp_path: Path,
):
    request = _request()
    provider = _Provider("winner", _candidate())
    materialized, reservation = _materialized(tmp_path, provider.candidate)
    transport = _Transport(materialized)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    first = asyncio.create_task(pipeline.deliver(request, DeliveryTarget("1")))
    await transport.started.wait()
    second = asyncio.create_task(pipeline.deliver(request, DeliveryTarget("2")))
    await asyncio.sleep(0)
    transport.release.set()
    await asyncio.gather(first, second)

    assert provider.calls == 1
    assert transport.calls == 1
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_materialization_failure_resolves_next_route_without_duplicate_heavy_work(
    tmp_path: Path,
):
    request = build_media_request("https://www.instagram.com/reel/one/")
    gallery_candidate = replace(_candidate("gallery-dl"), media_id="one")
    ytdlp_candidate = replace(_candidate("ytdlp"), media_id="one")
    gallery = _Provider("gallery-dl", gallery_candidate)
    ytdlp = _Provider("ytdlp", ytdlp_candidate, is_heavy=True)
    completed, reservation = _materialized(tmp_path, ytdlp_candidate)

    class FailingOverTransport:
        def __init__(self) -> None:
            self.providers: list[tuple[str | None, ...]] = []

        async def materialize(self, request, candidates, **kwargs):
            providers = tuple(candidate.provider for candidate in candidates)
            self.providers.append(providers)
            if providers == ("gallery-dl",):
                raise MaterializationError("expired CDN URL")
            return completed

    transport = FailingOverTransport()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(gallery), ProviderRoute(ytdlp)]),
        transport,
        _Delivery(),
        race_config=RaceConfig(heavy_delay=60),
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert receipt.success
    assert transport.providers == [("gallery-dl",), ("ytdlp",)]
    assert gallery.calls == 1
    assert ytdlp.calls == 1
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_materialization_reserve_does_not_repeat_earlier_cheap_routes(
    tmp_path: Path,
):
    request = build_media_request("https://www.instagram.com/reel/one/")
    unavailable = _Provider("snapsave", None)
    gallery_candidate = replace(_candidate("gallery-dl"), media_id="one")
    ytdlp_candidate = replace(_candidate("ytdlp"), media_id="one")
    gallery = _Provider("gallery-dl", gallery_candidate, is_heavy=True)
    ytdlp = _Provider("ytdlp", ytdlp_candidate, is_heavy=True)
    completed, _ = _materialized(tmp_path, ytdlp_candidate)

    class FailingOverTransport:
        async def materialize(self, request, candidates, **kwargs):
            if candidates[0].provider == "gallery-dl":
                raise MaterializationError("expired CDN URL")
            return completed

    pipeline = MediaPipeline(
        ProviderRegistry(
            [
                ProviderRoute(unavailable),
                ProviderRoute(gallery),
                ProviderRoute(ytdlp),
            ]
        ),
        FailingOverTransport(),
        _Delivery(),
        race_config=RaceConfig(heavy_delay=0),
        artifact_validator=AsyncMock(),
        enforce_route_matrix=True,
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert receipt.success
    assert unavailable.calls == 1
    assert gallery.calls == 1
    assert ytdlp.calls == 1


@pytest.mark.asyncio
async def test_public_entrypoints_share_resolve_and_materialize_flights(
    tmp_path: Path,
):
    private = replace(_request(), caller_scope="private")
    group = replace(_request(), caller_scope="group")
    provider = _Provider("winner", _candidate())
    materialized, reservation = _materialized(tmp_path, provider.candidate)
    transport = _Transport(materialized)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    first = asyncio.create_task(
        pipeline.deliver(private, DeliveryTarget("1", caller_scope="private"))
    )
    second = asyncio.create_task(
        pipeline.deliver(group, DeliveryTarget("2", caller_scope="group"))
    )
    await transport.started.wait()
    transport.release.set()
    receipts = await asyncio.gather(first, second)

    assert all(receipt.success for receipt in receipts)
    assert provider.calls == 1
    assert transport.calls == 1
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_uncertain_delivery_is_never_retried_and_lease_is_released(
    tmp_path: Path,
):
    request = _request()
    candidate = _candidate()
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = _Delivery()
    uncertain = DeliveredItem(
        item=MediaItem("abc123", MediaKind.VIDEO, candidate.url),
        status=DeliveryStatus.UNCERTAIN,
        telegram_type=MediaKind.VIDEO,
        error_category="unknown_outcome",
    )
    delivery.receipts.append(
        DeliveryReceipt(DeliveryTarget("1"), (uncertain,), DeliveryStatus.UNCERTAIN)
    )
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert receipt.status is DeliveryStatus.UNCERTAIN
    assert delivery.retry_calls == 0
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_slow_telegram_delivery_periodically_renews_materialized_lease(
    tmp_path: Path,
):
    request = _request()
    candidate = _candidate()
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()

    class SlowDelivery(_Delivery):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def deliver(self, media, target, **kwargs):
            self.started.set()
            await self.finish.wait()
            return await super().deliver(media, target, **kwargs)

    delivery = SlowDelivery()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
        lease_renew_interval=0.01,
    )

    task = asyncio.create_task(pipeline.deliver(request, DeliveryTarget("1")))
    await delivery.started.wait()
    await asyncio.sleep(0.035)
    delivery.finish.set()
    receipt = await task

    assert receipt.success
    assert reservation.renewed >= 3
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_telegram_lease_renewal_failure_stops_without_duplicate_send(
    tmp_path: Path,
):
    request = _request()
    candidate = _candidate()

    class FailingReservation(_Reservation):
        def renew(self) -> None:
            super().renew()
            if self.renewed >= 2:
                raise RuntimeError("lease owner changed")

    path = tmp_path / "item.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom")
    reservation = FailingReservation()
    materialized = MaterializedItem((path,), 12, candidate, reservation)
    transport = _Transport(materialized)
    transport.release.set()

    class HangingDelivery(_Delivery):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = False

        async def deliver(self, media, target, **kwargs):
            self.calls.append(media)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    delivery = HangingDelivery()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
        lease_renew_interval=0.01,
    )

    with pytest.raises(MediaPipelineError, match="outcome is uncertain"):
        await pipeline.deliver(request, DeliveryTarget("1"))

    assert len(delivery.calls) == 1
    assert delivery.retry_calls == 0
    assert delivery.cancelled
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_partial_delivery_retries_only_known_failed_items(tmp_path: Path):
    request = _request(kind=MediaKind.AUTO)
    items = (
        MediaItem("abc123:0", MediaKind.PHOTO, "https://cdn.example/0.jpg"),
        MediaItem("abc123:1", MediaKind.VIDEO, "https://cdn.example/1.mp4"),
        MediaItem("abc123:2", MediaKind.PHOTO, "https://cdn.example/2.jpg"),
    )
    candidate = _candidate(items=items)
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = _Delivery()
    initial = (
        DeliveredItem(items[0], DeliveryStatus.SUCCESS, MediaKind.PHOTO, 0),
        DeliveredItem(items[1], DeliveryStatus.FAILED, MediaKind.VIDEO, 1),
        DeliveredItem(items[2], DeliveryStatus.UNCERTAIN, MediaKind.PHOTO, 2),
    )
    retried = (DeliveredItem(items[1], DeliveryStatus.SUCCESS, MediaKind.VIDEO, 1),)
    delivery.receipts.extend(
        [
            DeliveryReceipt(DeliveryTarget("1"), initial, DeliveryStatus.PARTIAL),
            DeliveryReceipt(DeliveryTarget("1"), retried, DeliveryStatus.SUCCESS),
        ]
    )
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert delivery.retry_calls == 1
    assert tuple(item.item_index for item in receipt.items) == (0, 1, 2)
    assert receipt.items[0].status is DeliveryStatus.SUCCESS
    assert receipt.items[1].status is DeliveryStatus.SUCCESS
    assert receipt.items[2].status is DeliveryStatus.UNCERTAIN
    assert receipt.status is DeliveryStatus.PARTIAL
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_cached_partial_retries_failed_but_not_uncertain_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = _request(kind=MediaKind.AUTO)
    items = (
        MediaItem("abc123:0", MediaKind.PHOTO, "https://cdn.example/0.jpg"),
        MediaItem("abc123:1", MediaKind.VIDEO, "https://cdn.example/1.mp4"),
    )
    candidate = _candidate(items=items)
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = _Delivery()
    cached = DeliveryReceipt(
        DeliveryTarget("1"),
        (
            DeliveredItem(items[0], DeliveryStatus.UNCERTAIN, MediaKind.PHOTO, 0),
            DeliveredItem(items[1], DeliveryStatus.FAILED, MediaKind.VIDEO, 1),
        ),
        DeliveryStatus.PARTIAL,
    )
    delivery.receipts.append(
        DeliveryReceipt(
            DeliveryTarget("1"),
            (DeliveredItem(items[1], DeliveryStatus.SUCCESS, MediaKind.VIDEO, 1),),
            DeliveryStatus.SUCCESS,
        )
    )
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )
    monkeypatch.setattr(pipeline, "_deliver_cached", AsyncMock(return_value=cached))

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert delivery.retry_calls == 1
    assert tuple(item.status for item in receipt.items) == (
        DeliveryStatus.UNCERTAIN,
        DeliveryStatus.SUCCESS,
    )
    assert reservation.released == 1


@pytest.mark.asyncio
async def test_resolution_error_preserves_provider_specific_message():
    provider = _Provider("empty", None)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        SimpleNamespace(),
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    with pytest.raises(MediaResolutionError, match="empty"):
        await pipeline.resolve(_request())


@pytest.mark.asyncio
async def test_mixed_album_keeps_item_order_without_slideshow_conversion(
    tmp_path: Path,
):
    items = (
        MediaItem("abc123:0", MediaKind.PHOTO, "https://cdn.example/0.jpg"),
        MediaItem("abc123:1", MediaKind.VIDEO, "https://cdn.example/1.mp4"),
    )
    candidate = _candidate(items=items)
    materialized, _ = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = _Delivery()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    resolved = await pipeline.resolve(_request(kind=MediaKind.AUTO))
    receipt = await pipeline.deliver(_request(kind=MediaKind.AUTO), DeliveryTarget("1"))

    assert tuple(item.kind for item in resolved.items) == (
        MediaKind.PHOTO,
        MediaKind.VIDEO,
    )
    assert tuple(item.item.kind for item in receipt.items) == (
        MediaKind.PHOTO,
        MediaKind.VIDEO,
    )


@pytest.mark.asyncio
async def test_album_materialization_excludes_slideshow_soundtrack(tmp_path: Path):
    items = (
        MediaItem("abc123:0", MediaKind.PHOTO, "https://cdn.example/0.jpg"),
        MediaItem("abc123:1", MediaKind.PHOTO, "https://cdn.example/1.jpg"),
    )
    candidate = _candidate(items=items)
    candidate = replace(
        candidate,
        sources=(
            MediaSource("0", items[0].url, container="jpg"),
            MediaSource("1", items[1].url, container="jpg"),
            MediaSource("audio", "https://cdn.example/music.mp3", container="mp3"),
        ),
    )
    materialized, _ = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    await pipeline.deliver(_request(kind=MediaKind.AUTO), DeliveryTarget("1"))

    assert tuple(source.url for source in transport.seen_candidates[0].sources) == (
        items[0].url,
        items[1].url,
    )


@pytest.mark.asyncio
async def test_album_selection_projects_fresh_items_and_cached_output_indexes(
    tmp_path: Path,
):
    items = tuple(
        MediaItem(
            f"abc123:{index}",
            MediaKind.PHOTO,
            f"https://cdn.example/{index}.jpg",
        )
        for index in range(3)
    )
    candidate = replace(
        _candidate(items=items),
        sources=tuple(
            MediaSource(str(index), item.url, container="jpg")
            for index, item in enumerate(items)
        ),
    )
    request = replace(_request(kind=MediaKind.AUTO), album_selection=(2, 0))

    class ProjectingTransport(_Transport):
        async def materialize(self, request, candidates, **kwargs):
            selected = candidates[0]
            projected, reservation = _materialized(tmp_path, selected)
            self.item = projected
            self.reservation = reservation
            self.calls += 1
            self.seen_candidates = tuple(candidates)
            self.started.set()
            return projected

    cache = MediaCache()
    transport = ProjectingTransport(_materialized(tmp_path, candidate)[0])
    provider = _Provider("winner", candidate)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        _Delivery(cache),
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    selected = transport.seen_candidates[0]
    assert selected.items == (items[2], items[0])
    assert tuple(source.url for source in selected.sources) == (
        items[2].url,
        items[0].url,
    )
    assert tuple(item.item.media_id for item in receipt.items) == (
        "abc123:2",
        "abc123:0",
    )

    await cache.put_delivery(
        request,
        bot_id="bot-1",
        delivery=CachedDelivery("selected-0", MediaKind.PHOTO, 0),
    )
    await cache.put_delivery(
        request,
        bot_id="bot-1",
        delivery=CachedDelivery("selected-1", MediaKind.PHOTO, 1),
    )
    cached_provider = _Provider("never", candidate)
    cached_pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(cached_provider)]),
        transport,
        _Delivery(cache),
        artifact_validator=AsyncMock(),
    )

    cached_receipt = await cached_pipeline.deliver(request, DeliveryTarget("2"))

    assert cached_receipt.success
    assert tuple(item.item_index for item in cached_receipt.items) == (0, 1)
    assert cached_provider.calls == 0


@pytest.mark.asyncio
async def test_negative_album_selection_is_rejected_before_materialization(
    tmp_path: Path,
):
    items = tuple(
        MediaItem(
            f"abc123:{index}",
            MediaKind.PHOTO,
            f"https://cdn.example/{index}.jpg",
        )
        for index in range(2)
    )
    candidate = replace(_candidate(items=items), media_id="abc123")
    transport_item, _ = _materialized(tmp_path, candidate)
    transport = _Transport(transport_item)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    with pytest.raises(MediaResolutionError, match="album_selection_unavailable"):
        await pipeline.deliver(
            replace(_request(kind=MediaKind.AUTO), album_selection=(-1,)),
            DeliveryTarget("1"),
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_auto_request_without_album_metadata_does_not_accept_item_zero_cache(
    tmp_path: Path,
):
    request = _request(kind=MediaKind.AUTO)
    cache = MediaCache()
    await cache.put_delivery(
        request,
        bot_id="bot-1",
        delivery=CachedDelivery("possibly-partial", MediaKind.PHOTO, 0),
    )
    candidate = _candidate()
    materialized, _ = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    provider = _Provider("winner", candidate)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,
        _Delivery(cache),
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("1"))

    assert receipt.success
    assert provider.calls == 1
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_authorized_album_metadata_enables_file_id_hit_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    collector = MetricsCollector()
    monkeypatch.setattr(pipeline_module, "metrics", collector)
    request = build_media_request(
        "https://www.instagram.com/stories/tester/123/",
        kind=MediaKind.ALBUM,
        caller_scope="ig_callback",
        auth_scope="instagram:session-7",
    )
    items = (
        MediaItem("story-1", MediaKind.PHOTO, "https://cdn.example/1.jpg"),
        MediaItem("story-2", MediaKind.VIDEO, "https://cdn.example/2.mp4"),
    )
    candidate = MediaCandidate(
        candidate_id="authorized-album",
        url=items[0].url,
        has_video=True,
        has_audio=True,
        provider="authorized-instagram",
        backend_family="instagram-session",
        media_id=request.media_id,
        kind=MediaKind.ALBUM,
        items=items,
        sources=(
            MediaSource("0", items[0].url, container="jpg"),
            MediaSource("1", items[1].url, container="mp4"),
        ),
        auth_scope=request.auth_scope,
    )
    materialized, _ = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    cache = MediaCache()

    class CachingDelivery(_Delivery):
        async def deliver(self, media, target, **kwargs):
            receipt = await super().deliver(media, target, **kwargs)
            cache_request = kwargs["request"]
            for delivered in receipt.items:
                await cache.put_delivery(
                    cache_request,
                    bot_id=self.bot_id,
                    delivery=CachedDelivery(
                        delivered.file_id or f"file-{delivered.item_index}",
                        delivered.telegram_type,
                        delivered.item_index,
                    ),
                )
            return receipt

    pipeline = MediaPipeline(
        ProviderRegistry([]),
        transport,
        CachingDelivery(cache),
        artifact_validator=AsyncMock(),
    )
    target = DeliveryTarget(
        "1", caller_scope="ig_callback", auth_scope=request.auth_scope
    )

    first = await pipeline.deliver_candidate(request, target, candidate)
    second = await pipeline.deliver_candidate(request, target, candidate)

    assert first.success and second.success
    assert transport.calls == 1
    assert collector.pipeline_results.collect() == [
        ({"platform": "instagram", "status": "success"}, 2.0)
    ]
    total = [
        count
        for labels, count, _ in collector.pipeline_duration.collect()
        if labels.get("phase") == "total"
    ]
    assert total == [2]


@pytest.mark.asyncio
async def test_artifact_validation_failure_releases_materialized_lease(tmp_path: Path):
    candidate = _candidate()
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(side_effect=RuntimeError("invalid bytes")),
    )

    with pytest.raises(MediaPipelineError, match="winner: invalid bytes"):
        await pipeline.deliver(_request(), DeliveryTarget("1"))

    assert reservation.released == 1


@pytest.mark.asyncio
async def test_delivery_cancellation_is_raised_only_after_lease_release(tmp_path: Path):
    class BlockingDelivery(_Delivery):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def deliver(self, media, target, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

    candidate = _candidate()
    materialized, reservation = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    transport.release.set()
    delivery = BlockingDelivery()
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("winner", candidate))]),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
    )

    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("1")))
    await delivery.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reservation.released == 1


@pytest.mark.asyncio
async def test_authorized_request_rejects_public_candidate_before_materialization(
    tmp_path: Path,
):
    request = MediaRequest.from_url(
        "https://youtu.be/abc123", auth_scope="instagram:session"
    )
    candidate = _candidate()
    materialized, _ = _materialized(tmp_path, candidate)
    transport = _Transport(materialized)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(_Provider("authorized-instagram", candidate))]),
        transport,
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    with pytest.raises(MediaResolutionError, match="auth_scope_mismatch"):
        await pipeline.resolve(request)

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_delivery_target_cannot_cross_request_auth_scope(tmp_path: Path):
    request = MediaRequest.from_url(
        "https://youtu.be/abc123", auth_scope="instagram:session"
    )
    candidate = replace(_candidate(), auth_scope=request.auth_scope)
    materialized, _ = _materialized(tmp_path, candidate)
    provider = _Provider("authorized-instagram", candidate)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        _Transport(materialized),
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    with pytest.raises(MediaResolutionError, match="delivery auth scope"):
        await pipeline.deliver(request, DeliveryTarget("1", auth_scope="public"))

    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_request", "candidate", "probe", "message"),
    [
        (
            replace(_request(kind=MediaKind.AUDIO), audio_format="mp3"),
            replace(
                _candidate(),
                kind=MediaKind.AUDIO,
                has_video=False,
                container="mp3",
                mux_mode="extract-mp3",
            ),
            {
                "streams": [{"codec_type": "audio", "codec_name": "aac"}],
                "format": {"format_name": "mp3", "duration": "20"},
            },
            "MP3",
        ),
        (
            _request(kind=MediaKind.ANIMATION),
            replace(
                _candidate(),
                kind=MediaKind.ANIMATION,
                has_audio=False,
                mux_mode="mute-mp4",
            ),
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "mov,mp4", "duration": "20"},
            },
            "animation",
        ),
        (
            replace(
                _request(),
                clip=ClipInterval(start_seconds=10, end_seconds=20),
            ),
            _candidate(),
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "mov,mp4", "duration": "90"},
            },
            "clip duration",
        ),
        (
            _request(),
            _candidate(),
            {
                "streams": [
                    {"codec_type": "video", "width": 1280, "height": 720},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "matroska,webm", "duration": "20"},
            },
            "container",
        ),
    ],
)
async def test_final_artifact_validation_rejects_non_equivalent_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_request: MediaRequest,
    candidate: MediaCandidate,
    probe: dict[str, object],
    message: str,
):
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe", AsyncMock(return_value=probe)
    )

    with pytest.raises(ArtifactValidationError, match=message):
        await validate_materialized_artifact(media_request, materialized)


@pytest.mark.asyncio
async def test_final_artifact_validation_accepts_expected_clip_transcode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = replace(_request(), clip=ClipInterval(start_seconds=10, end_seconds=20))
    candidate = replace(
        _candidate(),
        container="mkv",
        sources=(
            MediaSource(
                "vp9-opus",
                "https://cdn.example/source.webm",
                video_codec="vp9",
                audio_codec="opus",
                container="webm",
            ),
        ),
    )
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe",
        AsyncMock(
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "matroska", "duration": "10.04"},
            }
        ),
    )

    await validate_materialized_artifact(request, materialized)


@pytest.mark.asyncio
async def test_final_artifact_rejects_landscape_output_for_portrait_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = replace(_candidate(), width=1080, height=1920)
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe",
        AsyncMock(
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "mov,mp4", "duration": "20"},
            }
        ),
    )

    with pytest.raises(ArtifactValidationError, match="dimensions"):
        await validate_materialized_artifact(_request(), materialized)


@pytest.mark.asyncio
async def test_final_artifact_applies_rotation_metadata_to_display_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = replace(_candidate(), width=1080, height=1920, duration_seconds=20)
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe",
        AsyncMock(
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "tags": {"rotate": "90"},
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "mov,mp4", "duration": "20.2"},
            }
        ),
    )

    await validate_materialized_artifact(_request(), materialized)


@pytest.mark.asyncio
async def test_final_artifact_rejects_truncated_unclipped_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = replace(_candidate(), duration_seconds=120)
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe",
        AsyncMock(
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"format_name": "mov,mp4", "duration": "20"},
            }
        ),
    )

    with pytest.raises(ArtifactValidationError, match="duration"):
        await validate_materialized_artifact(_request(), materialized)


@pytest.mark.asyncio
async def test_final_artifact_rejects_wrong_audio_language_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    request = replace(_request(), audio_language="uk")
    candidate = replace(_candidate(), audio_languages=("uk",))
    materialized, _ = _materialized(tmp_path, candidate)
    monkeypatch.setattr(
        "app.services.media.pipeline._ffprobe",
        AsyncMock(
            return_value={
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "tags": {"language": "eng"},
                    },
                ],
                "format": {"format_name": "mov,mp4", "duration": "20"},
            }
        ),
    )

    with pytest.raises(ArtifactValidationError, match="audio language"):
        await validate_materialized_artifact(request, materialized)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.tiktok.com/@u/video/1",
            ("tikwm", "ssstik", "cobalt", "gallery-dl", "ytdlp"),
        ),
        (
            "https://x.com/u/status/1",
            ("fxtwitter", "cobalt", "ytdlp"),
        ),
        (
            "https://www.instagram.com/reel/one/",
            ("snapsave", "cobalt", "gallery-dl", "ytdlp"),
        ),
        (
            "https://www.facebook.com/watch/?v=1",
            ("snapsave", "cobalt", "gallery-dl", "ytdlp"),
        ),
        (
            "https://www.pinterest.com/pin/1/",
            ("pinterest", "cobalt", "gallery-dl", "ytdlp"),
        ),
        (
            "https://www.youtube.com/watch?v=abc123",
            ("independent-youtube", "ytdlp"),
        ),
        ("https://vk.com/video1_2", ("ytdlp",)),
        ("https://rutube.ru/video/one/", ("ytdlp",)),
        ("https://media.example.net/watch/1", ("ytdlp",)),
    ],
)
def test_enforced_provider_route_matrix(url: str, expected: tuple[str, ...]):
    class Provider:
        backend_family = "test"
        is_heavy = False

        def __init__(self, name: str) -> None:
            self.name = name

        def supports(self, request: MediaRequest) -> bool:
            return True

        async def resolve(self, request: MediaRequest):
            return []

    names = (
        "tikwm",
        "ssstik",
        "fxtwitter",
        "snapsave",
        "pinterest",
        "cobalt",
        "independent-youtube",
        "gallery-dl",
        "ytdlp",
    )
    pipeline = MediaPipeline(
        ProviderRegistry(ProviderRoute(Provider(name)) for name in names),
        SimpleNamespace(),
        _Delivery(),
        enforce_route_matrix=True,
    )

    assert (
        tuple(
            route.provider.name
            for route in pipeline._routes_for(build_media_request(url))
        )
        == expected
    )


def test_default_pipeline_composes_gallery_dl_local_route(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "COBALT_API_URLS", [])
    monkeypatch.setattr(config, "SNAPSAVE_CONTRACT_VERIFIED", False)
    pipeline = build_default_pipeline(SimpleNamespace(id=1))

    routes = pipeline._routes_for(
        build_media_request("https://www.instagram.com/reel/one/")
    )

    assert tuple(route.provider.name for route in routes) == (
        "gallery-dl",
        "ytdlp",
    )


def test_default_pipeline_scopes_cobalt_credentials_to_exact_origins(monkeypatch):
    from app.core import config
    from app.services.media.providers.http import endpoint_headers

    monkeypatch.setattr(
        config,
        "COBALT_API_URLS",
        ["https://first.example", "https://second.example"],
    )
    monkeypatch.setattr(
        config,
        "COBALT_API_KEYS",
        {
            "https://first.example": "first-secret",
            "https://second.example": "second-secret",
        },
        raising=False,
    )
    monkeypatch.setattr(config, "COBALT_API_KEY", "")
    monkeypatch.setattr(config, "COBALT_CONTRACT_VERIFIED", True)
    pipeline = build_default_pipeline(SimpleNamespace(id=1))

    cobalt = next(
        route.provider
        for route in pipeline.registry._routes
        if route.provider.name == "cobalt"
    )

    assert [endpoint.origin for endpoint in cobalt.endpoints] == [
        "https://first.example",
        "https://second.example",
    ]
    assert [
        endpoint_headers(endpoint).get("Authorization") for endpoint in cobalt.endpoints
    ] == [
        "Api-Key first-secret",
        "Api-Key second-secret",
    ]


def test_default_pipeline_rejects_one_legacy_cobalt_key_for_multiple_origins(
    monkeypatch,
):
    from app.core import config

    monkeypatch.setattr(
        config,
        "COBALT_API_URLS",
        ["https://first.example", "https://second.example"],
    )
    monkeypatch.setattr(config, "COBALT_API_KEYS", {}, raising=False)
    monkeypatch.setattr(config, "COBALT_API_KEY", "shared-secret")
    monkeypatch.setattr(config, "COBALT_CONTRACT_VERIFIED", True)

    with pytest.raises(RuntimeError, match="exact origin"):
        build_default_pipeline(SimpleNamespace(id=1))


@pytest.mark.asyncio
async def test_disabled_provider_is_skipped_without_timeout():
    disabled = _Provider("tikwm", _candidate("tikwm"), delay=60)
    fallback = _Provider("ytdlp", replace(_candidate("ytdlp"), media_id="1"))
    pipeline = MediaPipeline(
        ProviderRegistry(
            [ProviderRoute(disabled, enabled=False), ProviderRoute(fallback)]
        ),
        SimpleNamespace(),
        _Delivery(),
        artifact_validator=AsyncMock(),
        enforce_route_matrix=True,
    )

    resolved = await asyncio.wait_for(
        pipeline.resolve(build_media_request("https://tiktok.com/@u/video/1")),
        timeout=0.25,
    )

    assert resolved.provider == "ytdlp"
    assert disabled.calls == 0


@pytest.mark.asyncio
async def test_race_rejects_wrong_media_identity_and_uses_matching_provider():
    wrong = _Provider("wrong", replace(_candidate("wrong"), media_id="other"))
    matching = _Provider("matching", _candidate("matching"), delay=0.01)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(wrong), ProviderRoute(matching)]),
        SimpleNamespace(),
        _Delivery(),
        artifact_validator=AsyncMock(),
    )

    resolved = await pipeline.resolve(_request())

    assert resolved.provider == "matching"
