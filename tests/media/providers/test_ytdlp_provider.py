import asyncio
import json
import time
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from app.services.media.models import MediaKind, MediaRequest, QualityPolicy
from app.services.media.providers.ytdlp import YtDlpProvider
from app.services.media.race import RaceConfig, race_candidates
from app.services.media.registry import FailureKind, ProviderError, ProviderRoute
from app.services.media.validation import validate_candidate
from app.services.ytdlp.exceptions import AccessDeniedError

URL = "https://www.youtube.com/watch?v=example"


def metadata(*, vertical=False, audio=True):
    formats = [
        {
            "format_id": "137",
            "url": "https://cdn.example/video?expire=1",
            "width": 1080 if vertical else 1920,
            "height": 1920 if vertical else 1080,
            "vcodec": "avc1.640028",
            "acodec": "none",
            "ext": "mp4",
            "filesize": 1000,
        },
        {
            "format_id": "136",
            "url": "https://cdn.example/720",
            "width": 1280,
            "height": 720,
            "vcodec": "avc1.64001f",
            "acodec": "none",
            "ext": "mp4",
            "filesize": 500,
        },
    ]
    if audio:
        formats.extend(
            [
                {
                    "format_id": "140",
                    "url": "https://cdn.example/audio",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "language": "en",
                    "filesize": 100,
                    "abr": 128,
                },
                {
                    "format_id": "140-uk",
                    "url": "https://cdn.example/audio-uk",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "language": "uk",
                    "filesize": 110,
                    "abr": 128,
                },
            ]
        )
    return {"id": "example", "title": "Example", "duration": 60, "formats": formats}


@pytest.mark.parametrize("edge,variant", [(720, "136+140"), (1080, "137+140")])
async def test_split_streams_offer_exact_quality_with_audio(edge, variant):
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    candidates = await provider.resolve(
        MediaRequest.from_url(URL, quality=QualityPolicy(edge))
    )
    assert candidates[0].candidate_id == variant
    candidate = candidates[0]
    assert candidate.has_audio and candidate.complete
    assert candidate.duration_seconds == 60
    assert candidate.sources[0].video_codec is not None
    assert candidate.sources[0].video_codec.startswith("avc1")
    assert candidate.sources[1].audio_codec == "mp4a.40.2"
    assert candidate.mux_mode == "copy"
    assert candidate.filesize_bytes == (600 if edge == 720 else 1100)
    assert candidate.audio_languages == ("en",)


async def test_vertical_shorts_keep_1080_short_edge():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata(vertical=True)))
    candidate = (
        await provider.resolve(MediaRequest.from_url(URL, quality=QualityPolicy(1080)))
    )[0]
    assert (candidate.width, candidate.height) == (1080, 1920)


async def test_missing_audio_cannot_produce_usable_video():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata(audio=False)))
    assert await provider.resolve(MediaRequest.from_url(URL)) == []


async def test_strict_mp3_selects_requested_language_and_requires_conversion():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    request = MediaRequest.from_url(
        URL, kind=MediaKind.AUDIO, audio_format="mp3", audio_language="uk"
    )
    candidate = (await provider.resolve(request))[0]
    assert candidate.candidate_id == "140-uk"
    assert candidate.audio_formats == ("mp3",)
    assert candidate.audio_languages == ("uk",)
    assert candidate.mux_mode == "extract-mp3"
    assert not candidate.has_video


async def test_expired_source_has_clean_refresh_descriptor_and_bounded_refresh():
    extract = AsyncMock(return_value=metadata())
    provider = YtDlpProvider(extract=extract)
    request = MediaRequest.from_url(URL, quality=QualityPolicy(1080))
    candidate = (await provider.resolve(request))[0]
    assert candidate.sources[0].expires_at == 1
    descriptor = candidate.refresh
    assert descriptor is not None
    serialized = json.dumps(asdict(descriptor))
    assert "cdn.example" not in serialized and "expire=" not in serialized
    refreshed = metadata()
    refreshed["formats"][0]["url"] = "https://cdn.example/fresh"
    extract.return_value = refreshed
    result = await provider.refresh(
        request, descriptor, attempt=0, deadline=time.monotonic() + 2
    )
    assert result.sources[0].url == "https://cdn.example/fresh"
    with pytest.raises(ProviderError):
        await provider.refresh(
            request, descriptor, attempt=1, deadline=time.monotonic() + 2
        )
    assert extract.await_count == 2


async def test_refresh_respects_existing_materialization_deadline():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    request = MediaRequest.from_url(URL)
    descriptor = (await provider.resolve(request))[0].refresh
    assert descriptor is not None
    with pytest.raises(ProviderError):
        await provider.refresh(
            request, descriptor, attempt=0, deadline=time.monotonic() - 1
        )


async def test_access_denied_is_transient_without_local_retry():
    extract = AsyncMock(side_effect=AccessDeniedError("bot check 403"))
    provider = YtDlpProvider(extract=extract)
    with pytest.raises(ProviderError) as caught:
        await provider.resolve(MediaRequest.from_url(URL))
    assert caught.value.kind is FailureKind.TRANSIENT
    assert extract.await_count == 1


async def test_cancellation_propagates():
    provider = YtDlpProvider(extract=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await provider.resolve(MediaRequest.from_url(URL))


async def test_external_route_can_win_after_local_youtube_403():
    failed = asyncio.Event()

    async def denied(url):
        failed.set()
        raise AccessDeniedError("403 bot check")

    class External:
        name = "external"
        backend_family = "independent"
        is_heavy = False

        def supports(self, request):
            return True

        async def resolve(self, request):
            await failed.wait()
            from app.services.media.models import MediaCandidate

            return [
                MediaCandidate(
                    "external-1080",
                    "https://external.example/video",
                    width=1920,
                    height=1080,
                )
            ]

    result = await race_candidates(
        MediaRequest.from_url(URL, quality=QualityPolicy(1080)),
        [ProviderRoute(YtDlpProvider(extract=denied)), ProviderRoute(External())],
        validate_candidate,
        config=RaceConfig(heavy_delay=0),
    )
    assert result.winner is not None
    assert result.winner.provider == "external"


async def test_unavailable_audio_language_is_not_silently_substituted():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    request = MediaRequest.from_url(URL, audio_language="fr")
    assert await provider.resolve(request) == []


async def test_muxed_wrong_language_is_not_advertised_as_requested_language():
    info = metadata()
    info["formats"][0]["acodec"] = "mp4a.40.2"
    info["formats"][0]["language"] = "en"
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))
    request = MediaRequest.from_url(
        URL, quality=QualityPolicy(1080), audio_language="uk"
    )
    assert await provider.resolve(request) == []
