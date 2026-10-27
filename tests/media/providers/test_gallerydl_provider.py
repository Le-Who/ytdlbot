from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.media.models import MediaKind, MediaRequest
from app.services.media.providers.gallerydl import GalleryDlProvider, _dump_gallery


async def test_gallery_dl_preserves_mixed_album_order_and_headers():
    async def extract(url: str):
        assert url == "https://www.instagram.com/p/album/"
        return [
            [2, {"title": "Album"}],
            [
                3,
                "https://cdn.example/first.jpg",
                {"id": "first", "extension": "jpg"},
            ],
            [
                3,
                "https://cdn.example/second.mp4",
                {
                    "id": "second",
                    "extension": "mp4",
                    "http_headers": {"Referer": "https://www.instagram.com/"},
                },
            ],
        ]

    request = MediaRequest(
        canonical_url="https://www.instagram.com/p/album/",
        platform="instagram",
        media_id="album",
        kind=MediaKind.AUTO,
    )

    candidates = await GalleryDlProvider(extract=extract).resolve(request)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind is MediaKind.ALBUM
    assert tuple(item.kind for item in candidate.items) == (
        MediaKind.PHOTO,
        MediaKind.VIDEO,
    )
    assert tuple(item.media_id for item in candidate.items) == ("first", "second")
    assert candidate.sources[1].http_headers == (
        ("referer", "https://www.instagram.com/"),
    )


@pytest.mark.parametrize("proxy", [None, "socks5://localhost:1080"])
async def test_gallery_route_ignores_environment_for_requests_and_embedded_ytdlp(
    monkeypatch, proxy
):
    import gallery_dl.ytdl as gallery_ytdl
    import yt_dlp
    from gallery_dl import config, extractor, option

    commands = []

    @asynccontextmanager
    async def run_subprocess(command):
        commands.append(command)
        yield SimpleNamespace(
            proc=SimpleNamespace(
                stdout=SimpleNamespace(read=AsyncMock(return_value=b"[]"))
            ),
            wait=AsyncMock(return_value=0),
        )

    monkeypatch.setattr(
        "app.services.media.providers.gallerydl.run_subprocess", run_subprocess
    )
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:18080")
    url = "https://www.tiktok.com/@user/video/123"
    await _dump_gallery(url, proxy=proxy)

    saved_config = copy.deepcopy(config._config)
    config.clear()
    try:
        args = option.build_parser().parse_args(commands[0][3:])
        for setting in args.options:
            config.set(*setting)
        instance = extractor.find(url)
        instance._init_options()
        instance._init_session()
        assert instance.session.trust_env is False
        assert instance._proxies == ({"http": proxy, "https": proxy} if proxy else None)
        user_options = {"proxy": instance._proxies["http"]} if instance._proxies else {}
        embedded = gallery_ytdl.construct_YoutubeDL(yt_dlp, instance, user_options)
        assert embedded.proxies == {"all": proxy or "__noproxy__"}
    finally:
        config.clear()
        config._config.update(saved_config)


def test_gallery_dl_is_public_and_platform_scoped():
    provider = GalleryDlProvider(extract=None)

    assert provider.supports(
        MediaRequest(
            canonical_url="https://www.pinterest.com/pin/1/",
            platform="pinterest",
            media_id="1",
        )
    )
    assert not provider.supports(
        MediaRequest(
            canonical_url="https://www.youtube.com/watch?v=abc123",
            platform="youtube",
            media_id="abc123",
        )
    )
    assert not provider.supports(
        MediaRequest(
            canonical_url="https://www.instagram.com/p/one/",
            platform="instagram",
            media_id="one",
            auth_scope="instagram:session",
        )
    )
