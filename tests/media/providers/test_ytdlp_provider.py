import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.media.models import MediaKind, MediaRequest, QualityPolicy
from app.services.media.pipeline import MediaPipeline, build_media_request
from app.services.media.providers.ytdlp import YtDlpProvider
from app.services.media.race import RaceConfig, race_candidates
from app.services.media.registry import (
    FailureKind,
    ProviderError,
    ProviderRegistry,
    ProviderRoute,
)
from app.services.media.validation import validate_candidate
from app.services.ytdlp.exceptions import AccessDeniedError
from app.services.ytdlp.service import YtDlpService

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


def video_format(
    format_id: str,
    *,
    width: int,
    height: int,
    vcodec: str = "avc1.640028",
    acodec: str = "none",
    ext: str = "mp4",
    filesize: int = 2_000,
):
    return {
        "format_id": format_id,
        "url": f"https://cdn.example/{format_id}",
        "width": width,
        "height": height,
        "vcodec": vcodec,
        "acodec": acodec,
        "ext": ext,
        "filesize": filesize,
    }


async def test_auto_request_uses_normal_video_resolution_path():
    """Catches the heavy fallback dropping requests whose kind was omitted."""
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    request = MediaRequest.from_url(URL, quality=QualityPolicy(1080))

    assert request.kind is MediaKind.AUTO
    assert provider.supports(request)
    candidate = (await provider.resolve(request))[0]
    assert candidate.has_video


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


@pytest.mark.parametrize(
    ("excluded_formats", "expected"),
    [
        ((), "136+140"),
        (("136",), "137+140"),
        (("136", "137"), "308+140"),
    ],
)
async def test_default_short_prefers_720_then_1080_through_pipeline(
    excluded_formats: tuple[str, ...], expected: str
):
    """Catches canonicalization and public work scope losing the Shorts profile."""
    info = metadata(vertical=True)
    info["formats"] = [
        fmt for fmt in info["formats"] if fmt["format_id"] not in excluded_formats
    ]
    info["formats"].insert(0, video_format("135", width=480, height=854, filesize=300))
    info["formats"].insert(
        0, video_format("308", width=1440, height=2560, filesize=16_000)
    )
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]), object(), object()
    )

    resolved = await pipeline.resolve(
        build_media_request(
            "https://www.youtube.com/shorts/example",
            caller_scope="private",
            exact=False,
        )
    )

    assert resolved.candidates[0].candidate_id == expected


@pytest.mark.parametrize(
    ("url", "quality", "expected"),
    [
        ("https://www.youtube.com/shorts/example", 1080, "137+140"),
        ("https://www.youtube.com/watch?v=example", None, "308+140"),
    ],
)
async def test_short_preference_does_not_change_explicit_or_watch_quality(
    url: str, quality: int | None, expected: str
):
    info = metadata(vertical=True)
    info["formats"].insert(
        0, video_format("308", width=1440, height=2560, filesize=16_000)
    )
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))
    pipeline = MediaPipeline(
        ProviderRegistry([ProviderRoute(provider)]), object(), object()
    )

    resolved = await pipeline.resolve(build_media_request(url, quality=quality))

    assert resolved.candidates[0].candidate_id == expected


async def test_fast_command_prefers_1080_source_over_4k():
    """Catches /mp4 downloading a needlessly large highest-resolution source."""
    info = metadata()
    info["formats"].insert(
        0,
        video_format("401", width=3840, height=2160, filesize=8_000),
    )
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))

    candidates = await provider.resolve(
        MediaRequest.from_url(URL, kind=MediaKind.VIDEO, caller_scope="command")
    )

    assert candidates[0].candidate_id == "137+140"
    assert min(candidates[0].width or 0, candidates[0].height or 0) == 1080
    assert any(candidate.candidate_id == "401+140" for candidate in candidates)


async def test_fast_command_prefers_ready_compatible_mp4_at_same_quality():
    """Catches /mp4 needlessly muxing when an equivalent ready MP4 exists."""
    info = metadata()
    info["formats"].append(
        video_format(
            "22",
            width=1920,
            height=1080,
            acodec="mp4a.40.2",
            filesize=1_050,
        )
    )
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))

    candidate = (
        await provider.resolve(
            MediaRequest.from_url(URL, kind=MediaKind.VIDEO, caller_scope="command")
        )
    )[0]

    assert candidate.candidate_id == "22"
    assert candidate.container == "mp4"
    assert candidate.mux_mode is None


async def test_explicit_quality_is_not_rewritten_by_fast_command_profile():
    """Catches the fast default overriding a user's explicit quality choice."""
    info = metadata()
    info["formats"].insert(
        0,
        video_format("401", width=3840, height=2160, filesize=8_000),
    )
    provider = YtDlpProvider(extract=AsyncMock(return_value=info))

    candidate = (
        await provider.resolve(
            MediaRequest.from_url(
                URL,
                kind=MediaKind.VIDEO,
                quality=QualityPolicy(2160),
                caller_scope="command",
            )
        )
    )[0]

    assert candidate.candidate_id == "401+140"


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


async def test_animation_request_selects_video_only_and_requires_mute_mp4():
    provider = YtDlpProvider(extract=AsyncMock(return_value=metadata()))
    request = MediaRequest.from_url(URL, kind=MediaKind.ANIMATION)

    assert provider.supports(request)
    candidate = (await provider.resolve(request))[0]

    assert candidate.kind is MediaKind.ANIMATION
    assert candidate.sources[0].video_codec is not None
    assert len(candidate.sources) == 1
    assert not candidate.has_audio
    assert candidate.container == "mp4"
    assert candidate.mux_mode == "mute-mp4"


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


@pytest.fixture
def extraction_commands_with_global_cookies(monkeypatch, tmp_path):
    cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-test-value\n"
    monkeypatch.setenv(
        "YTDLP_COOKIES_B64", base64.b64encode(cookie_text.encode()).decode()
    )
    # Exercise the real cookie manager, with its temporary files owned by pytest.
    monkeypatch.setattr("app.services.ytdlp.cookies.tempfile.tempdir", str(tmp_path))
    commands = []

    @asynccontextmanager
    async def run_subprocess(cmd, **kwargs):
        commands.append(cmd)
        yield SimpleNamespace(
            proc=SimpleNamespace(
                stdout=SimpleNamespace(
                    read=AsyncMock(return_value=json.dumps(metadata()).encode())
                ),
                returncode=0,
            ),
            wait=AsyncMock(),
            stderr_data=[],
        )

    # Replace only process I/O: provider, service, cookie lookup and CLI builder are real.
    monkeypatch.setattr("app.core.process.run_subprocess", run_subprocess)
    return commands


async def test_public_provider_omits_configured_global_cookies(
    extraction_commands_with_global_cookies,
):
    request = MediaRequest.from_url(URL)
    candidates = await YtDlpProvider().resolve(request)
    assert candidates
    assert "--cookies" not in extraction_commands_with_global_cookies[0]


async def test_public_provider_refresh_also_omits_global_cookies(
    extraction_commands_with_global_cookies,
):
    provider = YtDlpProvider()
    request = MediaRequest.from_url(URL)
    candidate = (await provider.resolve(request))[0]
    assert candidate.refresh is not None
    await provider.refresh(
        request, candidate.refresh, attempt=0, deadline=time.monotonic() + 2
    )
    assert len(extraction_commands_with_global_cookies) == 2
    assert all(
        "--cookies" not in cmd for cmd in extraction_commands_with_global_cookies
    )


async def test_nonpublic_scope_does_not_implicitly_authorize_global_cookies(
    extraction_commands_with_global_cookies,
):
    request = MediaRequest.from_url(URL, auth_scope="user:alice")
    await YtDlpProvider().resolve(request)
    assert "--cookies" not in extraction_commands_with_global_cookies[0]


@pytest.mark.parametrize(
    "scope,expected", [("public", False), ("user:alice", True), ("user:bob", False)]
)
async def test_only_explicitly_allowed_nonpublic_scope_uses_cookies(
    extraction_commands_with_global_cookies, scope, expected
):
    provider = YtDlpProvider(cookie_auth_scopes=frozenset({"public", "user:alice"}))
    await provider.resolve(MediaRequest.from_url(URL, auth_scope=scope))
    assert ("--cookies" in extraction_commands_with_global_cookies[0]) is expected


async def test_legacy_service_extract_preserves_configured_cookie_behavior(
    extraction_commands_with_global_cookies,
):
    await YtDlpService().extract(URL)
    assert "--cookies" in extraction_commands_with_global_cookies[0]
