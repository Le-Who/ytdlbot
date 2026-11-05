import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.media_cache import CachedDelivery, MediaCache
from app.services.media.models import DeliveryTarget, MediaKind
from app.services.media.pipeline import ArtifactValidationError, MediaPipeline
from app.services.media.proxies import MediaProxyPool
from app.services.media.registry import ProviderRegistry, ProviderRoute
from tests.media.test_pipeline import (
    _candidate,
    _Delivery,
    _materialized,
    _Provider,
    _request,
)


class RouteTransport:
    def __init__(self, items):
        self.items = items
        self.started = {name: asyncio.Event() for name in items}
        self.ready = {name: asyncio.Event() for name in items}
        self.cancelled = set()

    async def materialize(self, request, candidates, **kwargs):
        name = candidates[0].provider
        self.started[name].set()
        try:
            await self.ready[name].wait()
            return self.items[name]
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            await self.items[name].release(delete=True)
            self.cancelled.add(name)
            raise


@pytest.fixture
def setup_routes(tmp_path):
    providers = [_Provider(name, _candidate(name)) for name in ("direct", "proxy")]
    items = {}
    reservations = {}
    for provider in providers:
        directory = tmp_path / provider.name
        directory.mkdir()
        items[provider.name], reservations[provider.name] = _materialized(
            directory, provider.candidate
        )
    transport = RouteTransport(items)
    delivery = _Delivery()
    pipeline = MediaPipeline(
        ProviderRegistry(),
        transport,
        delivery,
        artifact_validator=AsyncMock(),
        youtube_routes=tuple(ProviderRoute(provider) for provider in providers),
    )
    return pipeline, transport, delivery, providers, reservations


async def entered(transport):
    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in transport.started.values())), 1
    )


async def test_first_completed_file_wins_and_joins_losing_download(
    setup_routes, caplog
):
    caplog.set_level("INFO")
    pipeline, transport, delivery, providers, reservations = setup_routes
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    transport.ready["proxy"].set()

    receipt = await asyncio.wait_for(task, 1)

    assert receipt.success
    assert len(delivery.calls) == 1
    assert delivery.calls[0].candidate.provider == "proxy"
    assert transport.cancelled == {"direct"}
    assert all(provider.calls == 1 for provider in providers)
    assert all(reservation.released == 1 for reservation in reservations.values())
    assert all(
        not path.exists() for item in transport.items.values() for path in item.paths
    )
    assert (
        sum(
            getattr(record, "op", "") == "media-measurement"
            for record in caplog.records
        )
        == 1
    )


async def test_invalid_fast_file_does_not_win(setup_routes):
    pipeline, transport, delivery, _, reservations = setup_routes

    async def validate(request, item):
        if item.candidate.provider == "direct":
            raise ArtifactValidationError("missing audio")

    pipeline.artifact_validator = validate
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    transport.ready["direct"].set()
    await asyncio.sleep(0)
    transport.ready["proxy"].set()
    assert (await asyncio.wait_for(task, 1)).success
    assert len(delivery.calls) == 1
    assert delivery.calls[0].candidate.provider == "proxy"
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_cancellation_joins_both_routes_without_delivery(setup_routes):
    pipeline, transport, delivery, _, reservations = setup_routes
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.cancelled == {"direct", "proxy"}
    assert delivery.calls == []
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_simultaneous_results_release_every_file_once(setup_routes):
    pipeline, transport, delivery, _, reservations = setup_routes
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    for ready in transport.ready.values():
        ready.set()
    assert (await asyncio.wait_for(task, 1)).success
    assert len(delivery.calls) == 1
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_cached_file_id_bypasses_both_routes(setup_routes):
    pipeline, _, delivery, providers, reservations = setup_routes
    cache = MediaCache()
    await cache.put_delivery(
        _request(),
        bot_id=delivery.bot_id,
        delivery=CachedDelivery("cached-file", MediaKind.VIDEO, 0),
    )
    pipeline.media_cache = cache
    assert (await pipeline.deliver(_request(), DeliveryTarget("123"))).success
    assert all(provider.calls == 0 for provider in providers)
    assert all(reservation.released == 0 for reservation in reservations.values())


async def test_two_callers_share_one_pair_of_route_attempts(setup_routes):
    pipeline, transport, delivery, providers, reservations = setup_routes
    tasks = [
        asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
        for _ in range(2)
    ]
    await entered(transport)
    transport.ready["direct"].set()
    receipts = await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert all(receipt.success for receipt in receipts)
    assert len(delivery.calls) == 2
    assert all(provider.calls == 1 for provider in providers)
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_cancellation_during_validation_releases_completed_files(setup_routes):
    pipeline, transport, delivery, _, reservations = setup_routes
    validating = asyncio.Event()

    async def validate(request, item):
        validating.set()
        await asyncio.Event().wait()

    pipeline.artifact_validator = validate
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    transport.ready["direct"].set()
    await asyncio.wait_for(validating.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delivery.calls == []
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_cancellation_while_loser_settles_also_releases_ready_winner(
    setup_routes,
):
    pipeline, transport, delivery, _, reservations = setup_routes
    cleanup_started = asyncio.Event()
    cleanup_allowed = asyncio.Event()
    original = transport.materialize

    async def materialize(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            cleanup_started.set()
            await cleanup_allowed.wait()
            raise

    transport.materialize = materialize
    task = asyncio.create_task(pipeline.deliver(_request(), DeliveryTarget("123")))
    await entered(transport)
    transport.ready["direct"].set()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    task.cancel()
    cleanup_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delivery.calls == []
    assert all(reservation.released == 1 for reservation in reservations.values())


async def test_default_pipeline_starts_direct_and_proxy_downloads(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from app.core import config, state
    from app.services.media.pipeline import build_default_pipeline, build_media_request
    from app.services.media.providers.ytdlp import YtDlpProvider
    from tests.media.providers.test_ytdlp_provider import metadata

    monkeypatch.setattr(config, "MEDIA_PROXY_URLS", ("socks5://proxy.example:1080",))
    monkeypatch.setattr(state, "redis_client", None)
    extraction_routes = []

    async def extract(self, request, proxy):
        extraction_routes.append(proxy)
        return metadata()

    monkeypatch.setattr(YtDlpProvider, "_extract_on_route", extract)
    pipeline = build_default_pipeline(SimpleNamespace(id=0))
    pipeline.media_cache = None
    started = {None: asyncio.Event(), "proxy-1": asyncio.Event()}
    cancelled = set()

    class Transport:
        async def materialize(self, request, candidates, **kwargs):
            candidate = candidates[0]
            route = candidate.sources[0].proxy_key
            started[route].set()
            if route is not None:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.add(route)
            await started["proxy-1"].wait()
            folder = tmp_path / "winner"
            folder.mkdir()
            return _materialized(folder, candidate)[0]

    pipeline.transport = Transport()
    pipeline.artifact_validator = AsyncMock()
    async with pipeline.open_materialized(
        build_media_request("https://www.youtube.com/shorts/example")
    ) as item:
        assert all(source.proxy_key is None for source in item.candidate.sources)
        assert cancelled == {"proxy-1"}
    assert None in extraction_routes
    assert "socks5://proxy.example:1080" in extraction_routes


def test_scoped_routes_share_health_without_crossing_network_paths():
    pool = MediaProxyPool(("socks5://proxy.example:1080",))
    direct = pool.for_routes((None,))
    proxied = pool.for_routes(("proxy-1",))
    assert direct.routes("youtube") == (None,)
    assert proxied.routes("youtube") == ("proxy-1",)
    direct.failed("youtube", None)
    assert direct.routes("youtube") == ()
    assert proxied.routes("youtube") == ("proxy-1",)
    pool.succeeded("youtube", None)
    assert direct.routes("youtube") == (None,)
