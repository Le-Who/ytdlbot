from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.core import state
from app.core.media_cache import CachedDelivery, MediaCache
from app.core.metrics import MetricsCollector
from app.services.media import delivery as delivery_module
from app.services.media import pipeline as pipeline_module
from app.services.media import race as race_module
from app.services.media import transport as transport_module
from app.services.media.delivery import DeliveryAsset, TelegramDelivery
from app.services.media.models import (
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
from app.services.media.pipeline import MediaPipeline
from app.services.media.registry import ProviderRegistry, ProviderRoute
from app.services.media.transport import MaterializedItem, MediaTransport


class _Reservation:
    def renew(self) -> None:
        pass

    async def release(self) -> None:
        pass

    def bind(self, path: Path) -> None:
        pass

    def unbind(self, path: Path) -> None:
        pass

    async def ensure(self, size: int) -> None:
        pass

    def promote(self, source: Path, destination: Path) -> None:
        source.replace(destination)


class _Provider:
    name = "metric-provider"
    is_heavy = False
    backend_family = "metric"

    def __init__(self, candidate: MediaCandidate) -> None:
        self.candidate = candidate
        self.calls = 0

    def supports(self, request: MediaRequest) -> bool:
        return True

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        self.calls += 1
        return [self.candidate]


class _Transport:
    def __init__(self, item: MaterializedItem) -> None:
        self.item = item
        self.calls = 0

    async def materialize(self, request, candidates, **kwargs):
        self.calls += 1
        return self.item


class _Delivery:
    bot_id = "bot-metrics"

    def __init__(self, cache: MediaCache | None = None) -> None:
        self.media_cache = cache

    async def deliver(self, media, target, **kwargs):
        if isinstance(media, tuple):
            item = media[0].item
        else:
            item = media.candidate.items[0]
        delivered = DeliveredItem(
            item=item,
            status=DeliveryStatus.SUCCESS,
            telegram_type=item.kind,
            file_id="file-metrics",
        )
        return DeliveryReceipt(target, (delivered,), DeliveryStatus.SUCCESS)

    async def retry_failed(self, receipt, media, target, **kwargs):
        return await self.deliver(media, target, **kwargs)


def _request() -> MediaRequest:
    return MediaRequest.from_url(
        "https://youtu.be/metrics01", kind=MediaKind.VIDEO, exact=True
    )


def _candidate() -> MediaCandidate:
    item = MediaItem(
        "metrics01", MediaKind.VIDEO, "https://cdn.example/video.mp4"
    )
    return MediaCandidate(
        candidate_id="metrics-720",
        url=item.url,
        has_video=True,
        has_audio=True,
        width=1280,
        height=720,
        container="mp4",
        provider="metric-provider",
        backend_family="metric",
        media_id="metrics01",
        kind=MediaKind.VIDEO,
        items=(item,),
    )


def test_media_pipeline_metrics_render_all_operational_series() -> None:
    collector = MetricsCollector()
    collector.pipeline_duration.observe(0.25, phase="resolve")
    collector.pipeline_duration.observe(1.25, phase="resolve")
    collector.pipeline_results.inc(status="success")
    collector.media_cache_events.inc(event="file_id_hit")
    collector.race_wasted_bytes.inc(4_096, provider="slow-provider")
    collector.retries.inc(reason="timeout")
    collector.provider_wins.inc(provider="fast-provider")
    collector.transcode_cpu_seconds.inc(1.5, operation="remux")
    collector.queue_depth.set(2, queue="download")
    collector.orphan_processes.set(1)

    rendered = collector.render()

    expected_series = (
        "ytdlbot_media_pipeline_duration_seconds",
        "ytdlbot_media_pipeline_results_total",
        "ytdlbot_media_cache_events_total",
        "ytdlbot_media_race_wasted_bytes_total",
        "ytdlbot_media_retries_total",
        "ytdlbot_media_provider_wins_total",
        "ytdlbot_media_transcode_cpu_seconds_total",
        "ytdlbot_media_queue_depth",
        "ytdlbot_media_orphan_processes",
    )
    assert all(series in rendered for series in expected_series)
    assert (
        'ytdlbot_media_pipeline_duration_seconds{phase="resolve",quantile="0.5"} '
        "0.250" in rendered
    )
    assert (
        'ytdlbot_media_pipeline_duration_seconds{phase="resolve",quantile="0.95"} '
        "1.250" in rendered
    )


def test_metrics_endpoint_refreshes_runtime_queue_orphan_and_profile_gauges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = MetricsCollector()
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "metrics", collector, raising=False)
    monkeypatch.setattr(state, "download_queue", SimpleNamespace(queue_depth=3))
    monkeypatch.setattr(state, "api_queue", SimpleNamespace(queue_depth=4))
    monkeypatch.setattr(
        state,
        "active_processes",
        [SimpleNamespace(returncode=0), SimpleNamespace(returncode=None)],
    )
    monkeypatch.setattr(routes.config, "APP_RELEASE", "sha-metrics")
    monkeypatch.setattr(routes.config, "MAX_MEDIA_FILE_MB", 2_000)
    monkeypatch.setattr(routes.config, "TELEGRAM_CLOUD_MAX_FILE_MB", 50)
    monkeypatch.setattr(
        routes.config, "TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081"
    )

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert 'ytdlbot_media_queue_depth{queue="download"} 3' in response.text
    assert 'ytdlbot_media_queue_depth{queue="api"} 4' in response.text
    assert "ytdlbot_media_orphan_processes 1" in response.text
    assert (
        'ytdlbot_delivery_profile_info{profile="local_bot_api",release="sha-metrics",'
        'upload_limit_mb="2000"} 1' in response.text
    )


@pytest.mark.asyncio
async def test_pipeline_records_resolve_materialize_deliver_result_and_provider_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = MetricsCollector()
    monkeypatch.setattr(pipeline_module, "metrics", collector)
    monkeypatch.setattr(race_module, "metrics", collector)
    candidate = _candidate()
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom")
    materialized = MaterializedItem(
        (path,), path.stat().st_size, candidate, _Reservation()  # type: ignore[arg-type]
    )
    provider = _Provider(candidate)
    transport = _Transport(materialized)
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        transport,  # type: ignore[arg-type]
        _Delivery(),  # type: ignore[arg-type]
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(_request(), DeliveryTarget("42"))

    assert receipt.success
    phases = {
        labels["phase"] for labels, _, _ in collector.pipeline_duration.collect()
    }
    assert {"resolve", "materialize", "deliver", "total"} <= phases
    assert collector.pipeline_results.collect() == [
        ({"platform": "youtube", "status": "success"}, 1.0)
    ]
    assert collector.provider_wins.collect() == [
        ({"platform": "youtube", "provider": "metric-provider"}, 1.0)
    ]


@pytest.mark.asyncio
async def test_pipeline_records_complete_file_id_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = MetricsCollector()
    monkeypatch.setattr(pipeline_module, "metrics", collector)
    request = _request()
    cache = MediaCache()
    await cache.put_delivery(
        request,
        bot_id="bot-metrics",
        delivery=CachedDelivery("cached-file", MediaKind.VIDEO, 0),
    )
    provider = _Provider(_candidate())
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]),
        _Transport(SimpleNamespace()),  # type: ignore[arg-type]
        _Delivery(cache),  # type: ignore[arg-type]
        artifact_validator=AsyncMock(),
    )

    receipt = await pipeline.deliver(request, DeliveryTarget("42"))

    assert receipt.success
    assert provider.calls == 0
    assert collector.media_cache_events.collect() == [
        ({"event": "file_id_hit", "platform": "youtube"}, 1.0)
    ]


@pytest.mark.asyncio
async def test_delivery_retry_and_transport_waste_and_transcode_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = MetricsCollector()
    monkeypatch.setattr(delivery_module, "metrics", collector)
    monkeypatch.setattr(transport_module, "metrics", collector)
    item = _candidate().items[0]
    failed = DeliveredItem(
        item=item,
        status=DeliveryStatus.FAILED,
        telegram_type=MediaKind.VIDEO,
        error="retry",
    )
    failed_receipt = DeliveryReceipt(
        DeliveryTarget("42"), (failed,), DeliveryStatus.FAILED
    )
    successful = DeliveryReceipt(
        DeliveryTarget("42"),
        (
            DeliveredItem(
                item=item,
                status=DeliveryStatus.SUCCESS,
                telegram_type=MediaKind.VIDEO,
            ),
        ),
        DeliveryStatus.SUCCESS,
    )
    delivery = TelegramDelivery(SimpleNamespace(id=1), local_mode=False)
    monkeypatch.setattr(delivery, "deliver", AsyncMock(return_value=successful))
    await delivery.retry_failed(
        failed_receipt,
        DeliveryAsset(item, tmp_path / "video.mp4", item_index=0),
        DeliveryTarget("42"),
    )

    async def run_process(command: list[str], timeout: float) -> int:
        del command, timeout
        await asyncio.sleep(0)
        return 0

    transport = MediaTransport(output_dir=tmp_path, process_runner=run_process)
    candidates = (_candidate(), _candidate())
    sizes = (111, 222)

    async def attempt(candidate: MediaCandidate) -> MaterializedItem:
        index = candidates.index(candidate)
        await asyncio.sleep(0)
        return MaterializedItem(
            (tmp_path / f"loser-{index}.mp4",),
            sizes[index],
            candidate,
            _Reservation(),  # type: ignore[arg-type]
        )

    winner, _ = await transport._race_small(
        candidates, attempt, transport.clock() + 1
    )
    assert winner is not None
    await winner.release(delete=True)
    await transport._run_transform(
        ["ffmpeg"], tmp_path / "partial.mp4", time.monotonic() + 1
    )

    assert collector.retries.collect() == [
        ({"backend": "telegram", "reason": "telegram_delivery"}, 1.0)
    ]
    assert sum(value for _, value in collector.race_wasted_bytes.collect()) in sizes
    assert collector.transcode_cpu_seconds.collect()[0][0] == {
        "measurement": "wall_time_proxy",
        "operation": "ffmpeg",
    }


@pytest.mark.asyncio
async def test_first_byte_latency_ignores_empty_chunks_and_has_bounded_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = MetricsCollector()
    monkeypatch.setattr(transport_module, "metrics", collector)

    class Response:
        status_code = 200
        headers = {"content-type": "video/mp4"}

        async def iter_bytes(self, chunk_size: int):
            del chunk_size
            yield b""
            yield b"\x00\x00\x00\x18ftypisom"

        async def close(self) -> None:
            pass

    transport = MediaTransport(output_dir=tmp_path)
    opened = SimpleNamespace(
        response=Response(),
        url="https://cdn.example/video.mp4?secret=never-a-label",
        started_at=transport.clock(),
    )
    monkeypatch.setattr(transport, "_request", AsyncMock(return_value=opened))

    _, written = await transport._stream_source(
        MediaSource(format_id="18", url=opened.url),
        _Reservation(),  # type: ignore[arg-type]
        already_written=0,
        deadline=transport.clock() + 1,
    )

    assert written > 0
    first_byte = [
        (labels, count)
        for labels, count, _ in collector.pipeline_duration.collect()
        if labels.get("phase") == "first_byte"
    ]
    assert first_byte == [({"phase": "first_byte"}, 1)]
