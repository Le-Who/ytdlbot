"""Proxy routing regressions: selection, URL affinity, cancellation and secrets."""

import asyncio
import ipaddress
import json
import logging
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from app.core.logging import JsonFormatter
from app.services.media.models import MediaRequest, RefreshDescriptor
from app.services.media.pipeline import _project_album_selection, build_media_request
from app.services.media.providers.gallerydl import GalleryDlProvider
from app.services.media.providers.ytdlp import YtDlpProvider
from app.services.media.proxies import MediaProxyPool
from app.services.media.registry import (
    FailureKind,
    ProviderError,
    ProviderRegistry,
    ProviderRoute,
)
from app.services.media.transport import (
    CurlStreamingClient,
    MediaTransport,
    UnsafeMediaURL,
    URLPolicy,
)
from app.services.ytdlp.exceptions import AccessDeniedError, VideoNotFoundError
from tests.media.providers.test_ytdlp_provider import metadata
from tests.media.test_transport import (
    FakeClient,
    FakeResponse,
    Resolver,
    request,
)
from tests.media.test_transport import (
    source_candidate as candidate,
)

PRIMARY = "socks5://alice:secret-one@proxy-one.example:8000"
BACKUP = "socks5://bob:secret-two@proxy-two.example:8000"


async def test_youtube_retries_backup_and_keeps_credentials_out_of_candidates():
    pool = MediaProxyPool((PRIMARY, BACKUP))
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        if proxy == PRIMARY:
            raise AccessDeniedError("blocked")
        return metadata()

    provider = YtDlpProvider(proxy_pool=pool, proxy_extract=extract)
    result = await provider.resolve(MediaRequest.from_url("https://youtu.be/example"))
    assert calls == [PRIMARY, BACKUP]
    assert {source.proxy_key for item in result for source in item.sources} == {
        "proxy-2"
    }
    serialized = json.dumps([asdict(item) for item in result])
    assert "secret-" not in serialized and "alice" not in serialized
    calls.clear()
    await provider.resolve(MediaRequest.from_url("https://youtu.be/example"))
    assert calls == [BACKUP]


async def test_permanent_error_does_not_retry_proxy():
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        raise VideoNotFoundError("removed")

    provider = YtDlpProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP)), proxy_extract=extract
    )
    with pytest.raises(ProviderError) as caught:
        await provider.resolve(MediaRequest.from_url("https://youtu.be/example"))
    assert caught.value.kind is FailureKind.PERMANENT
    assert calls == [PRIMARY]


async def test_proxy_timeout_cancels_attempt_before_backup():
    stopped = asyncio.Event()

    async def extract(url, proxy):
        if proxy == PRIMARY:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        assert stopped.is_set()
        return metadata()

    provider = YtDlpProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP), attempt_timeout=0.02),
        proxy_extract=extract,
    )
    result = await provider.resolve(MediaRequest.from_url("https://youtu.be/example"))
    assert result and stopped.is_set()


async def test_runtime_proxy_race_allows_backup_to_finish_after_slow_primary(
    monkeypatch,
):
    """The provider timer must cover the proxy pool, including its backup."""
    from app.core import config, state
    from app.services.media import pipeline as pipeline_module
    from app.services.media.race import race_candidates
    from tests.media.test_race import ManualClock

    clock = ManualClock()
    monkeypatch.setattr(config, "MEDIA_PROXY_URLS", (PRIMARY, BACKUP))
    monkeypatch.setattr(state, "redis_client", None)
    pipeline = pipeline_module.build_default_pipeline(SimpleNamespace(id=1))
    provider = pipeline.refreshers["ytdlp"]
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        if proxy == PRIMARY:
            await clock.sleep(20)
            raise ProviderError(FailureKind.TRANSIENT, "primary timed out")
        await clock.sleep(12)
        return metadata()

    async def race(request, routes, validate, *, config):
        return await race_candidates(
            request, routes, validate, config=config, clock=clock, sleep=clock.sleep
        )

    provider._proxy_extract = extract
    monkeypatch.setattr(pipeline_module, "race_candidates", race)
    task = asyncio.create_task(
        pipeline.resolve(build_media_request("https://youtu.be/example"))
    )
    await clock.advance(0)
    await clock.advance(20)
    await clock.advance(12)

    resolved = await asyncio.wait_for(task, timeout=1)
    assert calls == [PRIMARY, BACKUP]
    assert resolved.provider == "ytdlp"
    assert all(
        source.proxy_key == "proxy-2" for source in resolved.candidates[0].sources
    )


async def test_default_proxy_budget_allows_extraction_work_beyond_ten_seconds(
    monkeypatch,
):
    """Network timeouts must not also be the full subprocess lifetime."""
    from app.services.media import proxies
    from tests.media.test_race import ManualClock

    clock = ManualClock()

    async def wait_for(operation, *, timeout):
        attempt = asyncio.create_task(operation)
        timer = asyncio.create_task(clock.sleep(timeout))
        try:
            done, _ = await asyncio.wait(
                (attempt, timer), return_when=asyncio.FIRST_COMPLETED
            )
            if attempt in done:
                return attempt.result()
            raise TimeoutError
        finally:
            attempt.cancel()
            timer.cancel()
            await asyncio.gather(attempt, timer, return_exceptions=True)

    monkeypatch.setattr(proxies, "asyncio", SimpleNamespace(wait_for=wait_for))
    pool = MediaProxyPool((PRIMARY,), clock=clock)

    async def extract(proxy):
        await clock.sleep(12)
        return "resolved"

    task = asyncio.create_task(pool.extract("youtube", extract))
    await clock.advance(10)
    assert not task.done()
    await clock.advance(2)
    assert await asyncio.wait_for(task, timeout=1) == ("resolved", "proxy-1")


async def test_caller_cancellation_does_not_launch_backup():
    entered = asyncio.Event()
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        entered.set()
        await asyncio.Event().wait()

    provider = YtDlpProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP)), proxy_extract=extract
    )
    task = asyncio.create_task(
        provider.resolve(MediaRequest.from_url("https://youtu.be/example"))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [PRIMARY]


async def test_gallery_tries_direct_then_proxy_and_preserves_album_order():
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        if proxy is None:
            raise ProviderError(FailureKind.AUTH, "blocked")
        return [
            [3, "https://cdn.example/first.jpg", {"id": "1", "extension": "jpg"}],
            [3, "https://cdn.example/second.jpg", {"id": "2", "extension": "jpg"}],
        ]

    provider = GalleryDlProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP)), proxy_extract=extract
    )
    result = await provider.resolve(
        MediaRequest(
            canonical_url="https://www.instagram.com/p/example/",
            platform="instagram",
            media_id="example",
        )
    )
    assert calls == [None, PRIMARY]
    assert [item.media_id for item in result[0].items] == ["1", "2"]
    assert [source.proxy_key for source in result[0].sources] == ["proxy-1", "proxy-1"]


async def test_route_cooldown_is_platform_scoped_and_recovers():
    now = [0.0]
    pool = MediaProxyPool((PRIMARY, BACKUP), clock=lambda: now[0], cooldown=10)
    pool.failed("youtube", "proxy-1")
    assert pool.routes("youtube") == ("proxy-2",)
    assert pool.routes("instagram") == (None, "proxy-1", "proxy-2")
    now[0] = 11
    assert pool.routes("youtube") == ("proxy-1", "proxy-2")
    assert pool.routes("other") == (None,)


async def test_gallery_refresh_preserves_selected_subset_and_order():
    async def extract(url, proxy):
        return [
            [
                3,
                f"https://cdn.example/{index}.jpg",
                {"id": str(index), "extension": "jpg"},
            ]
            for index in range(3)
        ]

    provider = GalleryDlProvider(proxy_extract=extract)
    req = MediaRequest(
        canonical_url="https://www.instagram.com/p/example/",
        platform="instagram",
        media_id="example",
        album_selection=(2, 0),
    )
    original = (await provider.resolve(req))[0]
    fresh = await provider.refresh(
        req, original.refresh, attempt=0, deadline=__import__("time").monotonic() + 1
    )
    assert [item.media_id for item in fresh.items] == ["2", "0"]
    assert [source.url for source in fresh.sources] == [
        "https://cdn.example/2.jpg",
        "https://cdn.example/0.jpg",
    ]
    assert [source.format_id for source in fresh.sources] == ["0", "1"]


async def test_public_access_block_does_not_disable_platform_until_restart():
    async def extract(url, proxy):
        raise AccessDeniedError("blocked public request")

    provider = YtDlpProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP)), proxy_extract=extract
    )
    req = build_media_request("https://vk.com/video-1_2")
    registry = ProviderRegistry([ProviderRoute(provider)])
    with pytest.raises(ProviderError) as caught:
        await provider.resolve(req)
    registry.breaker.record_failure(provider.name, req.platform, caught.value)
    assert caught.value.kind is FailureKind.TRANSIENT
    assert registry.routes_for(req)


@pytest.mark.parametrize("host", ["x.com", "twitter.com"])
async def test_real_twitter_request_uses_proxy_fallback(host):
    calls = []

    async def extract(url, proxy):
        calls.append(proxy)
        if proxy is None:
            raise AccessDeniedError("blocked")
        return metadata()

    provider = YtDlpProvider(
        proxy_pool=MediaProxyPool((PRIMARY, BACKUP)), proxy_extract=extract
    )
    await provider.resolve(build_media_request(f"https://{host}/user/status/123"))
    assert calls == [None, PRIMARY]


async def test_selected_gallery_transfer_refreshes_selected_items_on_backup(tmp_path):
    pool = MediaProxyPool((PRIMARY, BACKUP))

    async def extract(url, proxy):
        if proxy is None:
            raise ProviderError(FailureKind.TRANSIENT, "blocked")
        return [
            [
                3,
                f"https://cdn.example/{index}.jpg",
                {"id": str(index), "extension": "jpg"},
            ]
            for index in range(3)
        ]

    provider = GalleryDlProvider(proxy_pool=pool, proxy_extract=extract)
    req = build_media_request(
        "https://instagram.com/p/example/", album_selection=(2, 0)
    )
    original = _project_album_selection(req, (await provider.resolve(req))[0])
    primary_client = FakeClient([FakeResponse(status_code=403)])
    backup_client = FakeClient([FakeResponse() for _ in range(4)])
    media = MediaTransport(
        output_dir=tmp_path,
        proxy_pool=pool,
        proxy_client_factory=lambda url: (
            primary_client if url == PRIMARY else backup_client
        ),
        url_policy=URLPolicy(resolver=Resolver({})),
    )
    item = await media.materialize(
        req, [original], refreshers={provider.name: provider}
    )
    assert [entry.media_id for entry in item.candidate.items] == ["2", "0"]
    assert len(item.paths) == 2
    assert all(source.proxy_key == "proxy-2" for source in item.candidate.sources)
    await item.release(delete=True)


async def test_transport_uses_same_proxy_for_probe_redirects_and_download(tmp_path):
    pool = MediaProxyPool((PRIMARY, BACKUP))
    direct = FakeClient([])
    proxied = FakeClient(
        [
            FakeResponse(
                status_code=302, headers={"location": "https://other.example/media"}
            ),
            FakeResponse(),
            FakeResponse(),
        ]
    )
    keys = []

    def factory(url):
        keys.append(url)
        return proxied

    media = MediaTransport(
        output_dir=tmp_path,
        client=direct,
        proxy_pool=pool,
        proxy_client_factory=factory,
        url_policy=URLPolicy(resolver=Resolver({})),
    )
    original = candidate()
    original = replace(
        original,
        sources=tuple(replace(s, proxy_key="proxy-1") for s in original.sources),
    )
    item = await media.materialize(request(), [original])
    await item.release(delete=True)
    assert not direct.requests
    assert len(proxied.requests) == 3
    assert keys and set(keys) == {PRIMARY}


async def test_proxy_redirect_still_rejects_private_destination(tmp_path):
    proxied = FakeClient(
        [
            FakeResponse(
                status_code=302, headers={"location": "http://private.example/x"}
            )
        ]
    )
    media = MediaTransport(
        output_dir=tmp_path,
        proxy_pool=MediaProxyPool((PRIMARY,)),
        proxy_client_factory=lambda url: proxied,
        url_policy=URLPolicy(resolver=Resolver({"private.example": ["127.0.0.1"]})),
    )
    with pytest.raises(UnsafeMediaURL):
        await media.probe("https://public.example/media", proxy_key="proxy-1")
    assert len(proxied.requests) == 1


async def test_transfer_failure_refreshes_urls_on_backup(tmp_path):
    pool = MediaProxyPool((PRIMARY, BACKUP))
    first = FakeClient([FakeResponse(status_code=403)])
    second = FakeClient([FakeResponse(), FakeResponse()])
    media = MediaTransport(
        output_dir=tmp_path,
        proxy_pool=pool,
        proxy_client_factory=lambda url: first if url == PRIMARY else second,
        url_policy=URLPolicy(resolver=Resolver({})),
    )
    original = candidate()
    original = replace(
        original,
        sources=tuple(replace(s, proxy_key="proxy-1") for s in original.sources),
        refresh=RefreshDescriptor("fixture", "1", original.candidate_id),
    )

    class Refresher:
        async def refresh(self, req, descriptor, *, attempt, deadline):
            assert pool.routes("youtube") == ("proxy-2",)
            return replace(
                original,
                sources=tuple(
                    replace(s, proxy_key="proxy-2", url="https://cdn.example/new")
                    for s in original.sources
                ),
            )

    item = await media.materialize(
        replace(request(), platform="youtube"),
        [original],
        refreshers={"fixture": Refresher()},
    )
    assert item.candidate.sources[0].proxy_key == "proxy-2"
    await item.release(delete=True)


def test_json_logs_redact_proxy_userinfo_in_message_fields_and_traceback():
    try:
        raise RuntimeError(PRIMARY)
    except RuntimeError:
        record = logging.LogRecord(
            "proxy",
            logging.ERROR,
            __file__,
            1,
            "proxy %s",
            (PRIMARY,),
            __import__("sys").exc_info(),
        )
    record.stderr = {"route": BACKUP}
    output = JsonFormatter().format(record)
    assert "secret-one" not in output and "secret-two" not in output
    assert "alice" not in output and "bob" not in output
    assert "proxy-one.example" in output


@pytest.mark.parametrize(
    "url",
    [
        "http://host:8000",
        "socks5://host",
        "socks5://host:bad",
        "socks5://host:8000/path",
        "socks5://host:8000?x=y",
    ],
)
def test_invalid_proxy_configuration_has_no_secret_in_error(url):
    with pytest.raises(ValueError) as caught:
        MediaProxyPool((url,))
    assert url not in str(caught.value)


async def test_real_curl_socks_uses_validated_ip_instead_of_remote_dns():
    """A local SOCKS peer observes the destination curl actually connects to."""
    destination = asyncio.Future()
    completed = asyncio.Event()

    async def socks_peer(reader, writer):
        try:
            greeting = await reader.readexactly(2)
            await reader.readexactly(greeting[1])
            writer.write(b"\x05\x00")
            await writer.drain()
            header = await reader.readexactly(4)
            if header[3] == 1:
                address = str(ipaddress.ip_address(await reader.readexactly(4)))
            else:
                address = "remote-dns"
                length = (await reader.readexactly(1))[0]
                await reader.readexactly(length)
            await reader.readexactly(2)
            destination.set_result(address)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            body = b"\x00\x00\x00\x18ftypisompayload"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            completed.set()

    server = await asyncio.start_server(socks_peer, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        pool = MediaProxyPool((f"socks5h://127.0.0.1:{port}",))
        client = CurlStreamingClient(pool.url("proxy-1"))
        response = await asyncio.wait_for(
            client.open(
                "GET",
                "http://unresolvable.invalid/video",
                headers={},
                resolved_ip=ipaddress.ip_address("8.8.8.8"),
                timeout=3,
            ),
            timeout=5,
        )
        try:
            assert await asyncio.wait_for(destination, 2) == "8.8.8.8"
            assert response.status_code == 200
        finally:
            await response.close()
        await asyncio.wait_for(completed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
