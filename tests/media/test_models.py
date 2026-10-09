from dataclasses import FrozenInstanceError, replace

import pytest

from app.services.media import (
    ClipInterval,
    MediaKind,
    MediaRequest,
    QualityPolicy,
    UnsupportedMediaUrlError,
    canonicalize_media_url,
)


def test_omitted_kind_is_auto_and_differs_from_explicit_video_in_cache():
    """Catches default requests silently imposing an explicit video constraint."""
    automatic = MediaRequest.from_url("https://youtu.be/abc123")
    explicit_video = MediaRequest.from_url(
        "https://youtu.be/abc123", kind=MediaKind.VIDEO
    )

    assert automatic.kind.value == "auto"
    assert automatic.cache_key != explicit_video.cache_key


def test_youtube_url_forms_share_identity_but_preserve_clip():
    """Catches identity keys that include aliases or discard clip boundaries."""
    short = MediaRequest.from_url("https://youtube.com/shorts/abc123?t=15")
    watch = MediaRequest.from_url("https://m.youtube.com/watch?v=abc123&utm_source=x")

    assert short.media_id == watch.media_id == "abc123"
    assert (
        short.canonical_url
        == watch.canonical_url
        == "https://www.youtube.com/watch?v=abc123"
    )
    assert short.clip == ClipInterval(start_seconds=15, end_seconds=None)
    assert short.cache_key != watch.cache_key


def test_youtube_tracking_parameters_do_not_change_request_identity():
    """Catches cache fragmentation caused by non-semantic query parameters."""
    plain = MediaRequest.from_url("https://youtu.be/abc123")
    tracked = MediaRequest.from_url(
        "https://www.youtube.com/watch?v=abc123&utm_source=mail&feature=share"
    )

    assert plain.cache_key == tracked.cache_key


def test_default_short_quality_keeps_distinct_cache_identity_from_watch():
    """Catches a watch URL reusing a 720p Shorts result, or vice versa."""
    short = MediaRequest.from_url("https://youtube.com/shorts/abc123")
    watch = MediaRequest.from_url("https://youtube.com/watch?v=abc123")

    assert short.canonical_url == watch.canonical_url
    assert short.cache_key != watch.cache_key


@pytest.mark.parametrize(
    ("url", "kind", "legacy_key"),
    [
        (
            "https://youtube.com/shorts/LSie-2eT6gw",
            MediaKind.AUTO,
            "media:v1:06f84f5841757bb00d69addbb61ef0f1a5995bbc58ae9346ce491c5eac2ef6f7",
        ),
        (
            "https://youtu.be/LSie-2eT6gw",
            MediaKind.AUTO,
            "media:v1:f7923175dd958d7cd4af10cfe1d3e4cade3bb0c547dfece81e62fb40fb34baf3",
        ),
        (
            "https://youtu.be/LSie-2eT6gw",
            MediaKind.AUDIO,
            "media:v1:17f64c73da4cae14ab1b0494baf01cabe5ae7bd80d35f76340809f617096bf5c",
        ),
    ],
)
def test_original_audio_policy_does_not_reuse_cached_youtube_dub(
    url: str, kind: MediaKind, legacy_key: str
):
    """Catches stale resolved media and Telegram file IDs hiding the audio fix."""
    request = MediaRequest.from_url(url, kind=kind)

    assert request.cache_key != legacy_key


@pytest.mark.parametrize(
    ("kind", "audio_language", "legacy_key"),
    [
        (
            MediaKind.VIDEO,
            "en",
            "media:v1:33aeb37802e429754e4307a8fc8c9d49b40f7e30d6046e9368ea86b217682bb0",
        ),
        (
            MediaKind.ANIMATION,
            None,
            "media:v1:a8351ad6ac67ca7a08898252c1a94c6f935df0f32598b2187a4836ed38ae6d88",
        ),
    ],
)
def test_original_audio_policy_keeps_unaffected_cache_entries(
    kind: MediaKind, audio_language: str | None, legacy_key: str
):
    request = MediaRequest.from_url(
        "https://youtu.be/LSie-2eT6gw", kind=kind, audio_language=audio_language
    )

    assert request.cache_key == legacy_key


@pytest.mark.parametrize(
    "change",
    [
        {"canonical_url": "https://www.youtube.com/watch?v=other"},
        {"platform": "other"},
        {"media_id": "other"},
        {"kind": MediaKind.AUDIO},
        {"quality": QualityPolicy(720)},
        {"audio_format": "mp3"},
        {"audio_language": "en"},
        {"clip": ClipInterval(1, 20)},
        {"clip": ClipInterval(0, 21)},
        {"album_selection": (1, 3)},
        {"watermark_allowed": True},
        {"caller_scope": "chat:2"},
        {"auth_scope": "user:2"},
        {"exact": False},
        {"output_variant": "custom"},
    ],
)
def test_cache_key_isolates_each_delivery_equivalence_field(change):
    """Catches cache reuse across auth boundaries or changed output requests."""
    base = MediaRequest.from_url(
        "https://youtu.be/abc123",
        quality=QualityPolicy(max_edge=1080),
        audio_format="m4a",
        audio_language="uk",
        clip=ClipInterval(0, 20),
        album_selection=(3, 1),
        watermark_allowed=False,
        caller_scope="chat:1",
        auth_scope="user:1",
        exact=True,
    )

    assert base.cache_key != replace(base, **change).cache_key
    assert base.cache_key.startswith("media:v1:")


def test_deadline_does_not_change_delivery_equivalence():
    request = MediaRequest.from_url("https://youtu.be/abc123")
    assert request.cache_key == replace(request, deadline=123).cache_key


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=abc123",
        "ftp://youtube.com/watch?v=abc123",
        "https://youtube.com:invalid/watch?v=abc123",
        "not a url",
        "https://youtube.com/watch",
    ],
)
def test_unsupported_or_malformed_urls_raise_typed_error(url: str):
    """Catches non-YouTube or incomplete URLs entering the canonical contract."""
    with pytest.raises(UnsupportedMediaUrlError):
        canonicalize_media_url(url)


@pytest.mark.parametrize("clip_value", ["NaN", "inf", "-inf"])
def test_non_finite_clip_time_raises_typed_error(clip_value: str):
    """Catches non-standard JSON cache identities from invalid clip times."""
    with pytest.raises(UnsupportedMediaUrlError):
        MediaRequest.from_url(f"https://youtube.com/watch?v=abc123&t={clip_value}")


def test_reversed_youtube_query_clip_raises_typed_error():
    with pytest.raises(UnsupportedMediaUrlError):
        MediaRequest.from_url("https://youtube.com/watch?v=abc123&t=20&end=10")


def test_contracts_are_immutable():
    """Catches mutable request state after a cache key has been computed."""
    request = MediaRequest.from_url("https://youtu.be/abc123")

    with pytest.raises(FrozenInstanceError):
        request.caller_scope = "chat:2"  # type: ignore[misc]
