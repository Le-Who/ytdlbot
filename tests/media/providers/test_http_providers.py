from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.services.media import MediaKind, MediaRequest, QualityPolicy
from app.services.media.providers.cobalt import CobaltProvider
from app.services.media.providers.fxtwitter import FxTwitterProvider
from app.services.media.providers.http import HttpResponse, ProviderEndpoint
from app.services.media.providers.snapsave import (
    SnapSaveProvider,
    UpstreamRenderRequired,
)
from app.services.media.providers.ssstik import SSSTikProvider
from app.services.media.providers.tikwm import TikWMProvider
from app.services.media.registry import FailureKind, ProviderError
from app.services.pinterest import PinterestProvider

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers"


@dataclass(frozen=True)
class RequestRecord:
    method: str
    url: str
    headers: dict[str, str]
    json: object | None
    data: object | None


class RecordingTransport:
    def __init__(self, *responses: HttpResponse | BaseException) -> None:
        self.responses = list(responses)
        self.requests: list[RequestRecord] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        data: object | None = None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(RequestRecord(method, url, headers or {}, json, data))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(
    *,
    status: int = 200,
    payload: object | None = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        json_data=payload,
        text=text,
    )


def fixture_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def request(
    platform: str,
    url: str,
    media_id: str,
    *,
    kind: MediaKind = MediaKind.VIDEO,
    quality: int | None = None,
) -> MediaRequest:
    return MediaRequest(
        canonical_url=url,
        platform=platform,
        media_id=media_id,
        kind=kind,
        quality=QualityPolicy(max_edge=quality),
    )


def endpoint(
    origin: str,
    platform: str,
    *,
    api_key: str | None = None,
    enabled: bool = True,
) -> ProviderEndpoint:
    return ProviderEndpoint(origin, api_key, frozenset({platform}), enabled)


def ok_probe() -> HttpResponse:
    return response(status=206, headers={"Content-Type": "video/mp4"})


@pytest.mark.asyncio
async def test_cobalt_error_on_one_origin_continues_to_next_origin():
    """Catches one unhealthy Cobalt instance suppressing a healthy fallback."""
    transport = RecordingTransport(
        response(payload={"status": "error", "error": {"code": "not_found"}}),
        response(
            payload={"status": "redirect", "url": "https://cdn.example/video.mp4"}
        ),
        ok_probe(),
    )
    provider = CobaltProvider(
        (
            endpoint("https://one.example", "tiktok"),
            endpoint("https://two.example", "tiktok"),
        ),
        transport=transport,
    )

    candidates = await provider.resolve(
        request("tiktok", "https://tiktok.com/@user/video/123", "123")
    )

    assert candidates[0].url == "https://cdn.example/video.mp4"
    assert [record.url for record in transport.requests[:2]] == [
        "https://one.example/",
        "https://two.example/",
    ]


@pytest.mark.asyncio
async def test_cobalt_credentials_are_not_forwarded_to_candidate_origin():
    """Catches endpoint-scoped API keys leaking into CDN requests."""
    transport = RecordingTransport(
        response(
            payload={"status": "redirect", "url": "https://cdn.other.example/video.mp4"}
        ),
        ok_probe(),
    )
    provider = CobaltProvider(
        (endpoint("https://api.one.example", "tiktok", api_key="secret"),),
        transport=transport,
    )

    await provider.resolve(
        request("tiktok", "https://tiktok.com/@user/video/123", "123")
    )

    assert transport.requests[0].headers["Authorization"] == "Api-Key secret"
    assert "Authorization" not in transport.requests[1].headers


@pytest.mark.asyncio
async def test_cobalt_credentials_are_stripped_on_cross_origin_http_redirect():
    """Catches the HTTP client forwarding endpoint credentials after a redirect."""
    transport = RecordingTransport(
        response(
            status=307,
            headers={"Location": "https://other.example/cobalt"},
        ),
        response(
            payload={"status": "redirect", "url": "https://cdn.example/video.mp4"}
        ),
        ok_probe(),
    )
    provider = CobaltProvider(
        (endpoint("https://api.one.example", "tiktok", api_key="secret"),),
        transport=transport,
    )

    await provider.resolve(
        request("tiktok", "https://tiktok.com/@user/video/123", "123")
    )

    assert transport.requests[0].headers["Authorization"] == "Api-Key secret"
    assert "Authorization" not in transport.requests[1].headers


@pytest.mark.asyncio
async def test_cobalt_picker_preserves_mixed_album_order_and_types():
    """Catches a provider collapsing an ordered album to one attachment."""
    transport = RecordingTransport(
        response(payload=fixture_json("cobalt-picker.json")),
        ok_probe(),
        ok_probe(),
        ok_probe(),
        ok_probe(),
    )
    provider = CobaltProvider(
        (endpoint("https://cobalt.example", "tiktok"),), transport=transport
    )

    candidate = (
        await provider.resolve(
            request(
                "tiktok",
                "https://tiktok.com/@user/photo/123",
                "123",
                kind=MediaKind.ALBUM,
            )
        )
    )[0]

    assert candidate.complete
    assert [item.url for item in candidate.items] == [
        "https://cdn.example/first.jpg",
        "https://cdn.example/second.mp4",
        "https://cdn.example/third.mp4",
    ]
    assert [item.kind for item in candidate.items] == [
        MediaKind.PHOTO,
        MediaKind.VIDEO,
        MediaKind.ANIMATION,
    ]


@pytest.mark.asyncio
async def test_tikwm_prefers_hd_url_even_when_hd_size_is_smaller():
    """Catches the invalid hd_size<size implies HEVC/BVC2 heuristic."""
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "hdplay": "https://cdn.tikwm.example/hd.mp4",
                    "play": "https://cdn.tikwm.example/sd.mp4",
                    "hd_size": 100,
                    "size": 200,
                    "width": 1080,
                    "height": 1920,
                },
            }
        ),
        ok_probe(),
    )
    provider = TikWMProvider(transport=transport)

    candidate = (
        await provider.resolve(
            request("tiktok", "https://tiktok.com/@u/video/9", "9", quality=1080)
        )
    )[0]

    assert candidate.url == "https://cdn.tikwm.example/hd.mp4"
    assert candidate.quality_label == "hd"
    assert not candidate.quality_limited


@pytest.mark.asyncio
async def test_tikwm_album_preserves_image_order():
    """Catches slideshow normalization reordering or dropping images."""
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "images": [
                        "https://cdn.tikwm.example/2.jpg",
                        "https://cdn.tikwm.example/1.jpg",
                    ],
                    "music": "https://cdn.tikwm.example/music.mp3",
                },
            }
        ),
        ok_probe(),
        ok_probe(),
        ok_probe(),
    )
    provider = TikWMProvider(transport=transport)

    candidate = (
        await provider.resolve(
            request(
                "tiktok",
                "https://tiktok.com/@u/photo/9",
                "9",
                kind=MediaKind.ALBUM,
            )
        )
    )[0]

    assert [item.url for item in candidate.items] == [
        "https://cdn.tikwm.example/2.jpg",
        "https://cdn.tikwm.example/1.jpg",
    ]
    assert candidate.has_audio


@pytest.mark.asyncio
async def test_tikwm_marks_sd_only_response_as_lower_quality():
    """Catches an SD fallback masquerading as the requested exact quality."""
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "play": "https://cdn.tikwm.example/sd.mp4",
                    "width": 576,
                    "height": 1024,
                },
            }
        ),
        ok_probe(),
    )

    candidate = (
        await TikWMProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/9", "9", quality=1080)
        )
    )[0]

    assert candidate.quality_label == "sd"
    assert candidate.quality_limited
    assert (candidate.width, candidate.height) == (576, 1024)


@pytest.mark.asyncio
async def test_fxtwitter_uses_v2_status_and_preserves_mixed_attachment_order():
    """Catches use of the legacy endpoint or grouped photo/video reordering."""
    transport = RecordingTransport(
        response(payload=fixture_json("fxtwitter-status.json")),
        ok_probe(),
        ok_probe(),
        ok_probe(),
    )
    provider = FxTwitterProvider(transport=transport, min_interval=0)

    candidate = (
        await provider.resolve(
            request(
                "twitter",
                "https://x.com/user/status/123456789",
                "123456789",
                kind=MediaKind.ALBUM,
            )
        )
    )[0]

    assert transport.requests[0].url == "https://api.fxtwitter.com/2/status/123456789"
    assert [item.kind for item in candidate.items] == [
        MediaKind.PHOTO,
        MediaKind.ANIMATION,
        MediaKind.VIDEO,
    ]
    assert candidate.items[-1].url == "https://video.twimg.com/third-720.mp4"
    assert candidate.items[-1].width == 1280
    assert candidate.complete


@pytest.mark.asyncio
async def test_fxtwitter_records_lower_quality_instead_of_claiming_equivalence():
    """Catches a 720p-only status silently satisfying an exact 1080 request."""
    payload = fixture_json("fxtwitter-status.json")
    payload["status"]["media"]["all"] = [payload["status"]["media"]["all"][2]]
    transport = RecordingTransport(response(payload=payload), ok_probe())

    candidate = (
        await FxTwitterProvider(transport=transport, min_interval=0).resolve(
            request(
                "twitter",
                "https://x.com/user/status/123456789",
                "123456789",
                quality=1080,
            )
        )
    )[0]

    assert (candidate.width, candidate.height) == (1280, 720)
    assert candidate.quality_limited


@pytest.mark.asyncio
async def test_ssstik_extracts_live_hidden_token_and_ready_link_from_html_fixtures():
    """Catches hard-coding the historical tt token name or a brittle regex."""
    transport = RecordingTransport(
        response(text=fixture_html("ssstik-index.html"), headers={"Content-Type": "text/html"}),
        response(text=fixture_html("ssstik-result.html"), headers={"Content-Type": "text/html"}),
        ok_probe(),
    )
    provider = SSSTikProvider(transport=transport)

    candidate = (
        await provider.resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )
    )[0]

    assert candidate.url == "https://cdn.ss.example/video.mp4"
    assert candidate.watermark_free is True
    assert transport.requests[1].data == {
        "id": "https://tiktok.com/@u/video/42",
        "locale": "en",
        "s_live_42": "fixture-session-token",
    }


@pytest.mark.asyncio
async def test_ssstik_rejects_cross_origin_form_action_without_leaking_credentials():
    """Catches an upstream HTML change exfiltrating endpoint key/session state."""
    transport = RecordingTransport(
        response(
            text=(
                '<form action="https://evil.example/collect">'
                '<input type="hidden" name="s_live" value="token">'
                '<input name="id"></form>'
            ),
            headers={
                "Content-Type": "text/html",
                "Set-Cookie": "session=private; Path=/",
            },
        )
    )
    provider = SSSTikProvider(
        endpoint("https://ssstik.example", "tiktok", api_key="secret"),
        transport=transport,
    )

    with pytest.raises(ProviderError):
        await provider.resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_tikwm_rejects_album_with_expired_audio_url():
    """Catches an album being marked complete when its soundtrack already expired."""
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "images": ["https://cdn.example/1.jpg"],
                    "music": "https://cdn.example/music.mp3?expire=10",
                },
            }
        ),
        ok_probe(),
    )
    provider = TikWMProvider(transport=transport, wall_clock=lambda: 20)

    with pytest.raises(ProviderError, match="expired"):
        await provider.resolve(
            request(
                "tiktok",
                "https://tiktok.com/@u/photo/42",
                "42",
                kind=MediaKind.ALBUM,
            )
        )


@pytest.mark.asyncio
async def test_snapsave_is_disabled_until_ready_link_contract_is_verified():
    """Catches an unverified changing web protocol entering the provider race."""
    provider = SnapSaveProvider(transport=RecordingTransport())

    assert not provider.supports(
        request("instagram", "https://instagram.com/reel/example", "example")
    )


@pytest.mark.asyncio
async def test_snapsave_ready_link_is_candidate_with_quality_metadata():
    """Catches treating a verified ready link as an opaque success."""
    transport = RecordingTransport(
        response(text=fixture_html("snapsave-ready.html"), headers={"Content-Type": "text/html"}),
        ok_probe(),
    )
    provider = SnapSaveProvider(transport=transport, contract_verified=True)

    candidate = (
        await provider.resolve(
            request(
                "instagram",
                "https://instagram.com/reel/example",
                "example",
                quality=1080,
            )
        )
    )[0]

    assert candidate.url == "https://cdn.snap.example/video.mp4"
    assert candidate.quality_label == "720p"
    assert candidate.quality_limited
    assert not candidate.remote_processing


@pytest.mark.asyncio
async def test_snapsave_upstream_render_job_is_not_a_ready_candidate():
    """Catches a remote render job ID being treated as a downloadable URL."""
    transport = RecordingTransport(
        response(
            text='<html><div data-job-id="render-123" data-status="processing"></div></html>',
            headers={"Content-Type": "text/html"},
        )
    )
    provider = SnapSaveProvider(transport=transport, contract_verified=True)

    with pytest.raises(UpstreamRenderRequired) as raised:
        await provider.resolve(
            request("facebook", "https://facebook.com/watch/?v=123", "123")
        )

    assert raised.value.job_id == "render-123"


def provider_cases(
    first: HttpResponse | BaseException,
) -> list[tuple[object, MediaRequest]]:
    return [
        (
            CobaltProvider(
                (endpoint("https://cobalt.example", "tiktok"),),
                transport=RecordingTransport(first),
            ),
            request("tiktok", "https://tiktok.com/@u/video/1", "1"),
        ),
        (
            TikWMProvider(transport=RecordingTransport(first)),
            request("tiktok", "https://tiktok.com/@u/video/1", "1"),
        ),
        (
            FxTwitterProvider(transport=RecordingTransport(first), min_interval=0),
            request("twitter", "https://x.com/u/status/1", "1"),
        ),
        (
            SSSTikProvider(transport=RecordingTransport(first)),
            request("tiktok", "https://tiktok.com/@u/video/1", "1"),
        ),
        (
            SnapSaveProvider(
                transport=RecordingTransport(first), contract_verified=True
            ),
            request("instagram", "https://instagram.com/reel/1", "1"),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,media_request", provider_cases(response(status=429, headers={"Retry-After": "7"})))
async def test_http_adapters_surface_retry_after(provider: object, media_request: MediaRequest):
    """Catches adapter-local retries hiding an upstream rate limit from the race."""
    with pytest.raises(ProviderError) as raised:
        await provider.resolve(media_request)  # type: ignore[attr-defined]

    assert raised.value.kind is FailureKind.TRANSIENT
    assert raised.value.retry_after == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,media_request", provider_cases(TimeoutError("slow upstream")))
async def test_http_adapters_classify_timeout(provider: object, media_request: MediaRequest):
    """Catches timeouts escaping without a circuit-breaker classification."""
    with pytest.raises(ProviderError) as raised:
        await provider.resolve(media_request)  # type: ignore[attr-defined]

    assert raised.value.kind is FailureKind.TRANSIENT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,media_request",
    provider_cases(
        response(
            text="<html><title>Just a moment...</title><div>Cloudflare challenge</div></html>",
            headers={"Content-Type": "text/html"},
        )
    ),
)
async def test_http_adapters_reject_challenge_instead_of_parsing_success(
    provider: object, media_request: MediaRequest
):
    """Catches challenge HTML being accepted as a resolver result."""
    with pytest.raises(ProviderError) as raised:
        await provider.resolve(media_request)  # type: ignore[attr-defined]

    assert raised.value.kind is FailureKind.TRANSIENT
    assert "challenge" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_expired_cdn_url_is_rejected_before_it_can_win_the_race():
    """Catches already-expired signed links becoming winning candidates."""
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {"hdplay": "https://cdn.example/video.mp4?expire=10"},
            }
        )
    )
    provider = TikWMProvider(transport=transport, wall_clock=lambda: 20)

    with pytest.raises(ProviderError) as raised:
        await provider.resolve(
            request("tiktok", "https://tiktok.com/@u/video/1", "1")
        )

    assert raised.value.kind is FailureKind.TRANSIENT
    assert "expired" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_pinterest_provider_wraps_native_resolution_without_downloading():
    """Catches the native provider bypassing the resolution-only contract."""
    calls: list[str] = []

    async def extract(url: str) -> tuple[str | None, str | None]:
        calls.append(url)
        return None, "https://i.pinimg.com/originals/asset.gif"

    provider = PinterestProvider(extract=extract)
    candidate = (
        await provider.resolve(
            request(
                "pinterest",
                "https://www.pinterest.com/pin/123",
                "123",
                kind=MediaKind.ANIMATION,
            )
        )
    )[0]

    assert calls == ["https://www.pinterest.com/pin/123"]
    assert candidate.kind is MediaKind.ANIMATION
    assert candidate.url.endswith(".gif")
    assert not provider.is_heavy
