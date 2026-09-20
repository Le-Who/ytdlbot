from dataclasses import FrozenInstanceError

import pytest

from app.services.media import (
    ClipInterval,
    MediaRequest,
    QualityPolicy,
    UnsupportedMediaUrlError,
    canonicalize_media_url,
)


def test_youtube_url_forms_share_identity_but_preserve_clip():
    """Catches identity keys that include aliases or discard clip boundaries."""
    short = MediaRequest.from_url("https://youtube.com/shorts/abc123?t=15")
    watch = MediaRequest.from_url("https://m.youtube.com/watch?v=abc123&utm_source=x")

    assert short.media_id == watch.media_id == "abc123"
    assert short.canonical_url == watch.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert short.clip == ClipInterval(start_seconds=15, end_seconds=None)
    assert short.cache_key != watch.cache_key


def test_youtube_tracking_parameters_do_not_change_request_identity():
    """Catches cache fragmentation caused by non-semantic query parameters."""
    plain = MediaRequest.from_url("https://youtu.be/abc123")
    tracked = MediaRequest.from_url(
        "https://www.youtube.com/watch?v=abc123&utm_source=mail&feature=share"
    )

    assert plain.cache_key == tracked.cache_key


def test_cache_key_isolates_scopes_and_all_delivery_equivalence_fields():
    """Catches cache reuse across auth boundaries or changed output requests."""
    base = MediaRequest.from_url(
        "https://youtu.be/abc123",
        quality=QualityPolicy(max_edge=1080),
        audio_format="m4a",
        audio_language="uk",
        album_selection=(3, 1),
        watermark_allowed=False,
        caller_scope="chat:1",
        auth_scope="user:1",
        exact=True,
    )

    assert base.cache_key != MediaRequest.from_url(
        "https://youtu.be/abc123", auth_scope="user:2"
    ).cache_key
    assert base.cache_key != MediaRequest.from_url(
        "https://youtu.be/abc123", exact=False
    ).cache_key
    assert base.cache_key.startswith("media:v1:")


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


def test_contracts_are_immutable():
    """Catches mutable request state after a cache key has been computed."""
    request = MediaRequest.from_url("https://youtu.be/abc123")

    with pytest.raises(FrozenInstanceError):
        request.caller_scope = "chat:2"  # type: ignore[misc]
