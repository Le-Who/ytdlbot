from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.services.media import MediaKind, MediaRequest, QualityPolicy
from app.services.media.pipeline import build_media_request
from app.services.media.providers.cobalt import CobaltProvider
from app.services.media.providers.fxtwitter import FxTwitterProvider
from app.services.media.providers.http import (
    CurlProviderTransport,
    HttpResponse,
    OriginPacer,
    ProviderEndpoint,
    parse_retry_after,
    probe_candidate,
    request_response,
)
from app.services.media.providers.snapsave import (
    SnapSaveProvider,
    UpstreamRenderRequired,
)
from app.services.media.providers.ssstik import SSSTikProvider
from app.services.media.providers.tikwm import TikWMProvider
from app.services.media.providers.ytdlp import YtDlpProvider
from app.services.media.race import RaceConfig, race_candidates
from app.services.media.registry import FailureKind, ProviderError, ProviderRoute
from app.services.media.transport import (
    CredentialRedirectError,
    DownloadFailed,
    MediaSizeExceeded,
    TransferTimeout,
    UnsafeMediaURL,
)
from app.services.media.validation import CandidateRejectionReason, validate_candidate
from app.services.pinterest import PinterestProvider

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_retry_after_rejects_nonfinite_delay(value):
    assert parse_retry_after({"Retry-After": value}) is None


@pytest.mark.asyncio
async def test_default_provider_probe_delegates_to_safe_streaming_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []

    async def safe_probe(self, url: str, *, headers=None, deadline=None):
        del self, deadline
        calls.append((url, headers))
        return b"\x00\x00\x00\x18ftyp"

    monkeypatch.setattr("app.services.media.transport.MediaTransport.probe", safe_probe)

    async def forbid_legacy_probe(*args, **kwargs):
        del args, kwargs
        raise AssertionError("legacy buffering probe used")

    monkeypatch.setattr(CurlProviderTransport, "request", forbid_legacy_probe)

    await probe_candidate(
        CurlProviderTransport(), "https://cdn.example/video.mp4?token=secret"
    )

    assert calls == [("https://cdn.example/video.mp4?token=secret", None)]


@pytest.mark.parametrize(
    "error,kind",
    [
        (TransferTimeout("media connection failed"), FailureKind.TRANSIENT),
        (DownloadFailed("media source temporarily unavailable"), FailureKind.TRANSIENT),
        (
            UnsafeMediaURL("media host resolved to a non-public address"),
            FailureKind.PERMANENT,
        ),
        (
            MediaSizeExceeded("declared media size exceeds configured limit"),
            FailureKind.PERMANENT,
        ),
        (CredentialRedirectError("cross-origin redirect"), FailureKind.CONFIG),
    ],
)
async def test_production_candidate_probe_classifies_transport_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, kind: FailureKind
) -> None:
    async def failed_probe(self, url: str, **kwargs):
        del self, url, kwargs
        raise error

    monkeypatch.setattr(
        "app.services.media.transport.MediaTransport.probe", failed_probe
    )

    with pytest.raises(ProviderError) as raised:
        await probe_candidate(CurlProviderTransport(), "https://cdn.example/video.mp4")

    assert raised.value.kind is kind
    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), RuntimeError("adapter bug")]
)
async def test_production_candidate_probe_preserves_cancellation_and_unknown_errors(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    async def failed_probe(self, url: str, **kwargs):
        del self, url, kwargs
        raise error

    monkeypatch.setattr(
        "app.services.media.transport.MediaTransport.probe", failed_probe
    )

    with pytest.raises(type(error)) as raised:
        await probe_candidate(CurlProviderTransport(), "https://cdn.example/video.mp4")

    assert raised.value is error


async def test_production_candidate_probe_preserves_server_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = DownloadFailed("media source temporarily unavailable", retry_after=7)

    async def failed_probe(self, url: str, **kwargs):
        del self, url, kwargs
        raise error

    monkeypatch.setattr(
        "app.services.media.transport.MediaTransport.probe", failed_probe
    )

    with pytest.raises(ProviderError) as raised:
        await probe_candidate(CurlProviderTransport(), "https://cdn.example/video.mp4")

    assert raised.value.kind is FailureKind.TRANSIENT
    assert raised.value.retry_after == 7


@pytest.mark.parametrize(
    "answer,kind",
    [
        (OSError("DNS temporarily unavailable"), FailureKind.TRANSIENT),
        ((), FailureKind.TRANSIENT),
        (("127.0.0.1",), FailureKind.PERMANENT),
    ],
)
async def test_production_candidate_probe_distinguishes_dns_failure_from_unsafe_answer(
    monkeypatch: pytest.MonkeyPatch,
    answer: OSError | tuple[str, ...],
    kind: FailureKind,
) -> None:
    async def resolve(host: str, port: int) -> tuple[str, ...]:
        del host, port
        if isinstance(answer, OSError):
            raise answer
        return answer

    monkeypatch.setattr("app.services.media.transport._system_resolver", resolve)

    with pytest.raises(ProviderError) as raised:
        await probe_candidate(CurlProviderTransport(), "https://cdn.example/video.mp4")

    assert raised.value.kind is kind
    assert isinstance(raised.value.__cause__, UnsafeMediaURL)


async def test_production_tikwm_probe_failure_recovers_in_same_provider_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ApiTransport(CurlProviderTransport):
        async def request(self, *args, **kwargs):
            return response(
                payload={"code": 0, "data": {"hdplay": "https://cdn.example/hd.mp4"}}
            )

    probes = 0

    async def probe(self, url: str, **kwargs):
        nonlocal probes
        del self, url, kwargs
        probes += 1
        if probes == 1:
            raise TransferTimeout("media connection failed")
        return b"\x00\x00\x00\x18ftyp"

    monkeypatch.setattr("app.services.media.transport.MediaTransport.probe", probe)
    media_request = request("tiktok", "https://tiktok.com/@u/video/42", "42")
    provider = TikWMProvider(
        endpoint("https://tikwm.example", "tiktok"), transport=ApiTransport()
    )

    result = await race_candidates(
        media_request,
        [ProviderRoute(provider)],
        validate_candidate,
        config=RaceConfig(total_timeout=2),
    )

    assert result.winner is not None
    assert result.winner.provider == "tikwm"
    assert probes == 2
    assert len(result.failures) == 1
    assert result.failures[0].kind is FailureKind.TRANSIENT


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
    min_interval: float = 0,
) -> ProviderEndpoint:
    return ProviderEndpoint(
        origin,
        api_key,
        frozenset({platform}),
        enabled,
        min_interval=min_interval,
    )


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
    assert transport.requests[1].json is None
    assert transport.requests[1].data is None


@pytest.mark.asyncio
async def test_cobalt_credentials_are_stripped_on_cross_origin_http_redirect():
    """Catches the HTTP client forwarding endpoint credentials after a redirect."""
    transport = RecordingTransport(
        response(
            status=303,
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
    assert transport.requests[1].json is None
    assert transport.requests[1].data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_status", [307, 308])
@pytest.mark.parametrize(
    "body",
    [
        {"data": {"s_live": "secret-token"}},
        {"json": {"token": "secret-token"}},
    ],
)
async def test_cross_origin_body_preserving_redirect_is_refused(
    body: dict[str, object], redirect_status: int
):
    """Catches form/JSON secrets surviving a cross-origin 307/308 redirect."""
    transport = RecordingTransport(
        response(
            status=redirect_status,
            headers={"Location": "https://evil.example/collect"},
        )
    )

    with pytest.raises(ProviderError, match="cross-origin"):
        await request_response(
            transport,
            "POST",
            "https://ssstik.example/submit",
            headers={"Authorization": "Api-Key secret"},
            **body,
        )

    assert [record.url for record in transport.requests] == [
        "https://ssstik.example/submit"
    ]
    assert not any(
        record.url.startswith("https://evil.example")
        and (record.data is not None or record.json is not None)
        for record in transport.requests
    )


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


def tikwm_video_response() -> HttpResponse:
    return response(
        payload={
            "code": 0,
            "data": {
                "hdplay": "https://cdn.example/hd.mp4",
                "play": "https://cdn.example/sd.mp4",
                "width": 576,
                "height": 1024,
                "hd_size": 200,
                "size": 100,
            },
        }
    )


async def test_tikwm_tries_distinct_sd_after_transient_hd_probe_failure():
    transport = RecordingTransport(
        tikwm_video_response(), ProviderError(FailureKind.TRANSIENT), ok_probe()
    )
    media_request = request(
        "tiktok", "https://tiktok.com/@u/video/42", "42", quality=1080
    )

    candidate = (await TikWMProvider(transport=transport).resolve(media_request))[0]

    assert candidate.url == "https://cdn.example/sd.mp4"
    assert candidate.quality_label == "sd"
    assert candidate.candidate_id == "tikwm:sd"
    assert candidate.filesize_bytes == 100
    assert candidate.quality_limited
    assert (
        CandidateRejectionReason.QUALITY_TOO_LOW
        in validate_candidate(media_request, candidate).reasons
    )


@pytest.mark.parametrize("quality", [None, 1080])
async def test_tikwm_sd_fallback_does_not_inherit_hd_dimensions(quality):
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "hdplay": "https://cdn.example/hd.mp4",
                    "play": "https://cdn.example/sd.mp4",
                    "width": 1080,
                    "height": 1920,
                },
            }
        ),
        ProviderError(FailureKind.TRANSIENT),
        ok_probe(),
    )
    media_request = request(
        "tiktok", "https://tiktok.com/@u/video/42", "42", quality=quality
    )

    candidate = (await TikWMProvider(transport=transport).resolve(media_request))[0]

    assert candidate.url == "https://cdn.example/sd.mp4"
    assert (candidate.width, candidate.height) == (None, None)
    assert (candidate.items[0].width, candidate.items[0].height) == (None, None)
    assert not candidate.metadata_complete
    assert candidate.quality_limited is (quality is not None)
    assert validate_candidate(media_request, candidate).usable is (quality is None)


@pytest.mark.parametrize(
    "kind", [FailureKind.AUTH, FailureKind.CONFIG, FailureKind.PERMANENT]
)
async def test_tikwm_does_not_try_sd_after_nontransient_hd_failure(kind):
    error = ProviderError(kind)
    transport = RecordingTransport(tikwm_video_response(), error, ok_probe())

    with pytest.raises(ProviderError) as raised:
        await TikWMProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value is error
    assert len(transport.requests) == 2


async def test_tikwm_preserves_preferred_probe_failure_when_all_alternatives_fail():
    error = ProviderError(FailureKind.TRANSIENT, "HD limited", retry_after=7)
    transport = RecordingTransport(
        tikwm_video_response(), error, ProviderError(FailureKind.TRANSIENT, "SD failed")
    )

    with pytest.raises(ProviderError) as raised:
        await TikWMProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value is error
    assert len(transport.requests) == 3


async def test_tikwm_does_not_probe_duplicate_hd_sd_url_twice():
    transport = RecordingTransport(
        response(
            payload={
                "code": 0,
                "data": {
                    "hdplay": "https://cdn.example/same.mp4",
                    "play": "https://cdn.example/same.mp4",
                },
            }
        ),
        ProviderError(FailureKind.TRANSIENT),
    )

    with pytest.raises(ProviderError):
        await TikWMProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert len(transport.requests) == 2


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
async def test_fxtwitter_selects_numbered_video_in_original_mixed_order():
    transport = RecordingTransport(
        response(payload=fixture_json("fxtwitter-status.json")),
        ok_probe(),
        ok_probe(),
        ok_probe(),
    )
    provider = FxTwitterProvider(transport=transport, min_interval=0)
    selected = build_media_request("https://x.com/user/status/123456789/video/3")

    (candidate,) = await provider.resolve(selected)

    assert candidate.kind is MediaKind.VIDEO
    assert [item.url for item in candidate.items] == [
        "https://video.twimg.com/third-720.mp4"
    ]
    assert len(transport.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("number", "ordered"), [(0, True), (1, True), (2, True), (3, False), (4, True)]
)
async def test_fxtwitter_rejects_unavailable_numbered_video(number, ordered):
    payload = fixture_json("fxtwitter-status.json")
    if not ordered:
        items = payload["status"]["media"]["all"]
        payload["status"]["media"] = {"photos": items[:1], "videos": items[1:]}
    transport = RecordingTransport(response(payload=payload))
    provider = FxTwitterProvider(transport=transport, min_interval=0)
    selected = build_media_request(
        f"https://x.com/user/status/123456789/video/{number}"
    )

    with pytest.raises(ProviderError):
        await provider.resolve(selected)

    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_numbered_x_video_uses_only_providers_that_respect_its_index():
    transport = RecordingTransport()
    endpoint = ProviderEndpoint("https://cobalt.example", None, frozenset({"x"}), True)
    cobalt = CobaltProvider([endpoint], transport=transport)
    ytdlp = YtDlpProvider()
    selected = build_media_request("https://x.com/user/status/123456789/video/3")
    zero = build_media_request("https://x.com/user/status/123456789/video/0")

    assert not cobalt.supports(selected)
    assert await cobalt.resolve(selected) == []
    assert transport.requests == []
    assert ytdlp.supports(selected)
    assert not ytdlp.supports(zero)


def test_numbered_x_video_does_not_reuse_whole_post_cache_entry():
    request = build_media_request("https://x.com/user/status/123456789/video/3")

    assert request.cache_key != (
        "media:v1:c9d7558fb9af5345bb3f8c983f44cb5c27d39bf048ba16450c8931cb530e83c9"
    )


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
async def test_fxtwitter_treats_zero_gif_duration_as_unknown():
    """FxTwitter reports zero for GIF duration when the value is unavailable."""
    payload = fixture_json("fxtwitter-status.json")
    gif = payload["status"]["media"]["all"][1]
    gif["duration"] = 0
    payload["status"]["media"]["all"] = [gif]
    transport = RecordingTransport(response(payload=payload), ok_probe())

    candidate = (
        await FxTwitterProvider(transport=transport, min_interval=0).resolve(
            request(
                "twitter",
                "https://x.com/user/status/123456789",
                "123456789",
                kind=MediaKind.ANIMATION,
            )
        )
    )[0]

    assert candidate.items[0].duration_seconds is None
    assert candidate.duration_seconds is None


@pytest.mark.asyncio
async def test_fxtwitter_falls_back_to_quoted_media_when_outer_status_is_empty():
    payload = fixture_json("fxtwitter-status.json")
    quoted_video = payload["status"]["media"]["all"][2]
    payload["status"]["media"] = {}
    payload["status"]["quote"] = {"media": {"all": [quoted_video]}}
    transport = RecordingTransport(response(payload=payload), ok_probe())

    candidate = (
        await FxTwitterProvider(transport=transport, min_interval=0).resolve(
            request(
                "twitter",
                "https://x.com/user/status/123456789",
                "123456789",
            )
        )
    )[0]

    assert candidate.url == "https://video.twimg.com/third-720.mp4"
    assert (candidate.width, candidate.height) == (1280, 720)


@pytest.mark.asyncio
async def test_ssstik_extracts_live_hidden_token_and_ready_link_from_html_fixtures():
    """Catches hard-coding the historical tt token name or a brittle regex."""
    transport = RecordingTransport(
        response(
            text=fixture_html("ssstik-index.html"),
            headers={"Content-Type": "text/html"},
        ),
        response(
            text=fixture_html("ssstik-result.html"),
            headers={"Content-Type": "text/html"},
        ),
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


def ssstik_two_link_responses() -> tuple[HttpResponse, HttpResponse]:
    return (
        response(text=fixture_html("ssstik-index.html")),
        response(
            text=fixture_html("ssstik-result.html")
            + '<a download href="https://cdn.ss.example/alternate.mp4">Without watermark 720p</a>'
        ),
    )


@pytest.mark.parametrize("failed_first", [False, True])
async def test_ssstik_retains_good_link_when_an_optional_alternative_fails(
    failed_first,
):
    failure = ProviderError(FailureKind.TRANSIENT, "candidate failed")
    probes = (failure, ok_probe()) if failed_first else (ok_probe(), failure)
    provider = SSSTikProvider(
        transport=RecordingTransport(*ssstik_two_link_responses(), *probes)
    )

    candidates = await provider.resolve(
        request("tiktok", "https://tiktok.com/@u/video/42", "42")
    )

    assert len(candidates) == 1
    expected = "alternate.mp4" if failed_first else "video.mp4"
    assert candidates[0].url == f"https://cdn.ss.example/{expected}"
    assert candidates[0].watermark_free is True


@pytest.mark.parametrize("kind", [FailureKind.AUTH, FailureKind.CONFIG])
async def test_ssstik_stops_optional_probes_on_hard_failure(kind):
    error = ProviderError(kind)
    transport = RecordingTransport(*ssstik_two_link_responses(), error, ok_probe())

    with pytest.raises(ProviderError) as raised:
        await SSSTikProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value is error
    assert len(transport.requests) == 3


async def test_ssstik_preserves_first_probe_failure_when_all_alternatives_fail():
    error = ProviderError(FailureKind.TRANSIENT, "rate limited", retry_after=7)
    transport = RecordingTransport(
        *ssstik_two_link_responses(), error, ProviderError(FailureKind.TRANSIENT)
    )

    with pytest.raises(ProviderError) as raised:
        await SSSTikProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value is error
    assert len(transport.requests) == 4


async def test_ssstik_all_failed_transient_alternative_remains_retryable():
    transport = RecordingTransport(
        *ssstik_two_link_responses(),
        ProviderError(FailureKind.PERMANENT, "oversized candidate"),
        ProviderError(FailureKind.TRANSIENT, "connection failed"),
    )

    with pytest.raises(ProviderError) as raised:
        await SSSTikProvider(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value.kind is FailureKind.TRANSIENT


@pytest.mark.parametrize("provider_type", [TikWMProvider, SSSTikProvider])
async def test_tiktok_all_failed_alternatives_preserve_later_retry_after(provider_type):
    metadata = (
        (tikwm_video_response(),)
        if provider_type is TikWMProvider
        else ssstik_two_link_responses()
    )
    transport = RecordingTransport(
        *metadata,
        ProviderError(FailureKind.TRANSIENT, "connection failed"),
        ProviderError(FailureKind.TRANSIENT, "rate limited", retry_after=7),
    )

    with pytest.raises(ProviderError) as raised:
        await provider_type(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert raised.value.kind is FailureKind.TRANSIENT
    assert raised.value.retry_after == 7


@pytest.mark.parametrize("provider_type", [TikWMProvider, SSSTikProvider])
async def test_tiktok_optional_probe_cancellation_does_not_try_another_link(
    provider_type,
):
    metadata = (
        (tikwm_video_response(),)
        if provider_type is TikWMProvider
        else ssstik_two_link_responses()
    )
    transport = RecordingTransport(*metadata, asyncio.CancelledError(), ok_probe())

    with pytest.raises(asyncio.CancelledError):
        await provider_type(transport=transport).resolve(
            request("tiktok", "https://tiktok.com/@u/video/42", "42")
        )

    assert len(transport.requests) == len(metadata) + 1


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
        response(
            text=fixture_html("snapsave-ready.html"),
            headers={"Content-Type": "text/html"},
        ),
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
@pytest.mark.parametrize(
    "provider,media_request",
    provider_cases(response(status=429, headers={"Retry-After": "7"}))
    + provider_cases(response(status=503, headers={"Retry-After": "7"})),
)
async def test_http_adapters_surface_retry_after(
    provider: object, media_request: MediaRequest
):
    """Catches adapter-local retries hiding an upstream rate limit from the race."""
    with pytest.raises(ProviderError) as raised:
        await provider.resolve(media_request)  # type: ignore[attr-defined]

    assert raised.value.kind is FailureKind.TRANSIENT
    assert raised.value.retry_after == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,media_request", provider_cases(TimeoutError("slow upstream"))
)
async def test_http_adapters_classify_timeout(
    provider: object, media_request: MediaRequest
):
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
        await provider.resolve(request("tiktok", "https://tiktok.com/@u/video/1", "1"))

    assert raised.value.kind is FailureKind.TRANSIENT
    assert "expired" in str(raised.value).lower()


@pytest.mark.asyncio
async def test_pinterest_provider_wraps_native_resolution_without_downloading():
    """Catches the native provider bypassing the resolution-only contract."""
    calls: list[str] = []

    async def extract(url: str) -> tuple[str | None, str | None]:
        calls.append(url)
        return None, "https://i.pinimg.com/originals/asset.gif"

    transport = RecordingTransport(ok_probe())
    provider = PinterestProvider(extract=extract, transport=transport)
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
    assert [record.url for record in transport.requests] == [candidate.url]


@pytest.mark.asyncio
async def test_pinterest_provider_rejects_expired_native_url_before_probe():
    """Catches Pinterest bypassing signed-URL expiry checks."""

    async def extract(_: str) -> tuple[str | None, str | None]:
        return "https://v.pinimg.com/video.mp4?expire=10", None

    transport = RecordingTransport()
    provider = PinterestProvider(
        extract=extract,
        transport=transport,
        wall_clock=lambda: 20,
    )

    with pytest.raises(ProviderError, match="expired") as raised:
        await provider.resolve(request("pinterest", "https://pinterest.com/pin/1", "1"))

    assert raised.value.kind is FailureKind.TRANSIENT
    assert transport.requests == []


@pytest.mark.asyncio
async def test_pinterest_provider_preserves_typed_transient_extraction_failure():
    """Catches transient native failures collapsing into permanent no-media."""

    async def extract(_: str) -> tuple[str | None, str | None]:
        raise ProviderError(FailureKind.TRANSIENT, "challenge")

    provider = PinterestProvider(extract=extract, transport=RecordingTransport())

    with pytest.raises(ProviderError) as raised:
        await provider.resolve(request("pinterest", "https://pinterest.com/pin/1", "1"))

    assert raised.value.kind is FailureKind.TRANSIENT


class PacingClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for when, future in list(self.sleepers):
            if when <= self.now and not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_origin_pacer_serializes_one_origin_without_blocking_another():
    """Catches a global limiter coupling unrelated provider origins."""
    clock = PacingClock()
    pacer = OriginPacer(clock=clock, sleep=clock.sleep)

    await pacer.wait("https://one.example", 2)
    same = asyncio.create_task(pacer.wait("https://one.example", 2))
    other = asyncio.create_task(pacer.wait("https://two.example", 2))
    await asyncio.sleep(0)

    assert other.done()
    assert not same.done()
    await clock.advance(2)
    await same


class RecordingPacer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def wait(self, origin: str, min_interval: float) -> None:
        self.calls.append((origin, min_interval))


@pytest.mark.asyncio
async def test_cobalt_uses_endpoint_specific_pacing():
    pacer = RecordingPacer()
    transport = RecordingTransport(
        response(
            payload={"status": "redirect", "url": "https://cdn.example/video.mp4"}
        ),
        ok_probe(),
    )
    provider = CobaltProvider(
        (endpoint("https://cobalt.example", "tiktok", min_interval=0.7),),
        transport=transport,
        pacer=pacer,
    )

    await provider.resolve(request("tiktok", "https://tiktok.com/@u/video/1", "1"))

    assert pacer.calls == [("https://cobalt.example", 0.7)]


@pytest.mark.asyncio
async def test_tikwm_default_route_applies_configured_pacing():
    pacer = RecordingPacer()
    transport = RecordingTransport(
        response(payload={"code": 0, "data": {"hdplay": "https://cdn.example/v.mp4"}}),
        ok_probe(),
    )
    provider = TikWMProvider(transport=transport, pacer=pacer)

    await provider.resolve(request("tiktok", "https://tiktok.com/@u/video/1", "1"))

    assert pacer.calls == [(provider.endpoint.origin, 1.1)]


@pytest.mark.asyncio
async def test_ssstik_paces_each_request_to_its_configured_origin():
    pacer = RecordingPacer()
    transport = RecordingTransport(
        response(
            text=fixture_html("ssstik-index.html"),
            headers={"Content-Type": "text/html"},
        ),
        response(
            text=fixture_html("ssstik-result.html"),
            headers={"Content-Type": "text/html"},
        ),
        ok_probe(),
    )
    provider = SSSTikProvider(
        endpoint("https://ssstik.example", "tiktok", min_interval=0.5),
        transport=transport,
        pacer=pacer,
    )

    await provider.resolve(request("tiktok", "https://tiktok.com/@u/video/1", "1"))

    assert pacer.calls == [
        ("https://ssstik.example", 0.5),
        ("https://ssstik.example", 0.5),
    ]


@pytest.mark.asyncio
async def test_snapsave_uses_endpoint_specific_pacing():
    pacer = RecordingPacer()
    transport = RecordingTransport(
        response(
            text=fixture_html("snapsave-ready.html"),
            headers={"Content-Type": "text/html"},
        ),
        ok_probe(),
    )
    provider = SnapSaveProvider(
        endpoint("https://snapsave.example", "instagram", min_interval=0.9),
        transport=transport,
        pacer=pacer,
        contract_verified=True,
    )

    await provider.resolve(request("instagram", "https://instagram.com/reel/1", "1"))

    assert pacer.calls == [("https://snapsave.example", 0.9)]
