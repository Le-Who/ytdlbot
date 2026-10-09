import asyncio
import ipaddress
from dataclasses import replace

import pytest

from app.services.media.models import MediaRequest
from app.services.media.providers.ytdlp import YtDlpProvider, _source
from app.services.media.proxies import MediaProxyPool
from app.services.media.transport import (
    CurlStreamingClient,
    DownloadFailed,
    MediaTransport,
    UnsafeMediaURL,
    URLPolicy,
)
from tests.media.providers.test_ytdlp_provider import metadata
from tests.media.test_transport import (
    FakeClient,
    FakeResponse,
    Resolver,
    request,
    source_candidate,
)


def test_ytdlp_preserves_bounded_http_download_hint():
    fmt = metadata()["formats"][0]
    fmt["downloader_options"] = {"http_chunk_size": 10485760}
    assert _source(fmt, metadata()).http_chunk_size == 10485760


async def test_playlist_formats_are_not_exposed_as_direct_streams():
    info = metadata()
    playlist = dict(info["formats"][0], format_id="hls", protocol="m3u8_native")
    info["formats"].append(playlist)

    async def extract(url):
        return info

    candidates = await YtDlpProvider(extract=extract).resolve(
        MediaRequest.from_url("https://youtu.be/example")
    )
    assert candidates
    assert all(
        source.format_id != "hls"
        for candidate in candidates
        for source in candidate.sources
    )


async def test_playlist_only_metadata_cannot_bypass_protocol_filter():
    info = metadata()
    info["formats"] = [dict(info["formats"][0], protocol="m3u8_native")]
    info.update(url="https://cdn.example/playlist.m3u8", protocol="m3u8_native")

    async def extract(url):
        return info

    assert not await YtDlpProvider(extract=extract).resolve(
        MediaRequest.from_url("https://youtu.be/example")
    )


async def test_range_download_joins_exact_bytes_and_preserves_route(tmp_path):
    payload = b"\x00\x00\x00\x18ftypisom" + b"abcdefghijklmnopqrstuvwx"
    size = len(payload)
    responses = [FakeResponse()]
    for start in range(0, size, 12):
        end = min(size - 1, start + 11)
        responses.append(
            FakeResponse(
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(end - start + 1),
                },
                chunks=(payload[start : end + 1],),
            )
        )
    client = FakeClient(responses)
    direct = FakeClient([])
    media = MediaTransport(
        output_dir=tmp_path,
        client=direct,
        proxy_pool=MediaProxyPool(("socks5://proxy.example:8000",)),
        proxy_client_factory=lambda url: client,
        url_policy=URLPolicy(resolver=Resolver({})),
    )
    candidate = source_candidate(size=size)
    candidate = replace(
        candidate,
        sources=tuple(
            replace(
                source,
                http_chunk_size=12,
                proxy_key="proxy-1",
                http_headers=(("accept-encoding", "gzip"),),
            )
            for source in candidate.sources
        ),
    )
    item = await media.materialize(request(), [candidate])
    assert item.paths[0].read_bytes() == payload
    assert [entry["headers"]["Range"] for entry in client.requests[1:]] == [
        "bytes=0-11",
        "bytes=12-23",
        "bytes=24-35",
    ]
    assert client.active == 0
    assert not direct.requests
    assert all(
        entry["headers"]["Accept-Encoding"] == "identity"
        and "accept-encoding" not in entry["headers"]
        for entry in client.requests[1:]
    )
    await item.release(delete=True)


async def test_encoded_range_is_rejected_before_consuming_body(tmp_path):
    response = FakeResponse(
        status_code=206,
        headers={"Content-Range": "bytes 0-11/36", "Content-Encoding": "gzip"},
        chunks=(b"\x00\x00\x00\x18ftypisom",),
    )
    client = FakeClient([FakeResponse(), response])
    media = MediaTransport(
        output_dir=tmp_path, client=client, url_policy=URLPolicy(resolver=Resolver({}))
    )
    candidate = source_candidate(size=36)
    candidate = replace(
        candidate,
        sources=tuple(
            replace(source, http_chunk_size=12) for source in candidate.sources
        ),
    )
    with pytest.raises(DownloadFailed, match="encoding"):
        await media.materialize(request(), [candidate])
    assert not list(tmp_path.glob("media_*"))
    assert client.active == 0


@pytest.mark.parametrize(
    "second_header,second_body",
    [
        ("bytes 12-23/99", b"x" * 12),
        ("bytes 13-24/36", b"x" * 12),
        ("bytes 12-23/36", b"x" * 11),
    ],
)
async def test_inconsistent_or_truncated_range_cleans_partial_file(
    tmp_path, second_header, second_body
):
    client = FakeClient(
        [
            FakeResponse(),
            FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-11/36"},
                chunks=(b"\x00\x00\x00\x18ftypisom",),
            ),
            FakeResponse(
                status_code=206,
                headers={"Content-Range": second_header},
                chunks=(second_body,),
            ),
        ]
    )
    media = MediaTransport(
        output_dir=tmp_path, client=client, url_policy=URLPolicy(resolver=Resolver({}))
    )
    candidate = source_candidate(size=36)
    candidate = replace(
        candidate,
        sources=tuple(
            replace(source, http_chunk_size=12) for source in candidate.sources
        ),
    )
    with pytest.raises(DownloadFailed):
        await media.materialize(request(), [candidate])
    assert not list(tmp_path.glob("media_*"))
    assert client.active == 0


async def test_server_ignoring_range_still_downloads_complete_response(tmp_path):
    payload = b"\x00\x00\x00\x18ftypisom" + b"x" * 24
    client = FakeClient([FakeResponse(), FakeResponse(chunks=(payload,))])
    media = MediaTransport(
        output_dir=tmp_path, client=client, url_policy=URLPolicy(resolver=Resolver({}))
    )
    candidate = source_candidate(size=len(payload))
    candidate = replace(
        candidate,
        sources=tuple(
            replace(source, http_chunk_size=12) for source in candidate.sources
        ),
    )
    item = await media.materialize(request(), [candidate])
    assert item.paths[0].read_bytes() == payload
    assert len(client.requests) == 2
    await item.release(delete=True)


async def test_changed_etag_rejects_mixed_range_content(tmp_path):
    client = FakeClient(
        [
            FakeResponse(),
            FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-11/36", "ETag": '"first"'},
                chunks=(b"\x00\x00\x00\x18ftypisom",),
            ),
            FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 12-23/36", "ETag": '"changed"'},
                chunks=(b"x" * 12,),
            ),
        ]
    )
    media = MediaTransport(
        output_dir=tmp_path, client=client, url_policy=URLPolicy(resolver=Resolver({}))
    )
    candidate = source_candidate(size=36)
    candidate = replace(
        candidate,
        sources=tuple(
            replace(source, http_chunk_size=12) for source in candidate.sources
        ),
    )
    with pytest.raises(DownloadFailed, match="validator"):
        await media.materialize(request(), [candidate])
    assert client.requests[-1]["headers"]["If-Range"] == '"first"'
    assert not list(tmp_path.glob("media_*"))
    assert client.active == 0


async def test_later_range_redirect_still_rejects_private_destination(tmp_path):
    client = FakeClient(
        [
            FakeResponse(),
            FakeResponse(
                status_code=206,
                headers={"Content-Range": "bytes 0-11/36"},
                chunks=(b"\x00\x00\x00\x18ftypisom",),
            ),
            FakeResponse(
                status_code=302, headers={"Location": "http://private.example/media"}
            ),
        ]
    )
    media = MediaTransport(
        output_dir=tmp_path,
        client=client,
        url_policy=URLPolicy(resolver=Resolver({"private.example": ["127.0.0.1"]})),
    )
    candidate = source_candidate(size=36)
    candidate = replace(
        candidate,
        sources=tuple(
            replace(source, http_chunk_size=12) for source in candidate.sources
        ),
    )
    with pytest.raises(UnsafeMediaURL):
        await media.materialize(request(), [candidate])
    assert client.active == 0 and len(client.requests) == 3
    assert not list(tmp_path.glob("media_*"))


async def test_real_curl_close_aborts_unconsumed_response():
    closed = asyncio.Event()
    handlers = []

    async def server_peer(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\npartial-body"
            )
            await writer.drain()
            await reader.read()
            closed.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(server_peer, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        response = await CurlStreamingClient().open(
            "GET",
            f"http://unresolvable.invalid:{port}/media",
            headers={},
            resolved_ip=ipaddress.ip_address("127.0.0.1"),
            timeout=5,
        )
        assert await anext(response.iter_bytes(100)) == b"partial-body"
        await asyncio.wait_for(response.close(), timeout=1)
        await asyncio.wait_for(closed.wait(), timeout=1)
    finally:
        for task in handlers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()
