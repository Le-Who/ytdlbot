from app.services.media import (
    CandidateRejectionReason,
    MediaCandidate,
    MediaItem,
    MediaKind,
    MediaRequest,
    QualityPolicy,
    validate_candidate,
)


def candidate_fixture(**changes: object) -> MediaCandidate:
    values: dict[str, object] = {
        "candidate_id": "fmt-137",
        "url": "https://cdn.example/video.mp4",
        "width": 1920,
        "height": 1080,
        "has_video": True,
        "has_audio": True,
        "audio_languages": ("uk",),
    }
    values.update(changes)
    return MediaCandidate(**values)


def request_fixture(**changes: object) -> MediaRequest:
    return MediaRequest.from_url("https://youtu.be/abc123", **changes)


def test_vertical_1080x1920_satisfies_1080_quality():
    """Catches quality selection based only on height, which rejects vertical video."""
    candidate = candidate_fixture(width=1080, height=1920)

    assert validate_candidate(
        request_fixture(quality=QualityPolicy(max_edge=1080)), candidate
    ).usable


def test_rejected_candidate_has_machine_readable_reason():
    """Catches an opaque validation result when a candidate misses requested audio."""
    request = request_fixture(audio_language="uk")
    candidate = candidate_fixture(audio_languages=("en",))

    result = validate_candidate(request, candidate)

    assert not result.usable
    assert CandidateRejectionReason.AUDIO_LANGUAGE_UNAVAILABLE in result.reasons


def test_exact_audio_request_rejects_candidate_without_that_format():
    """Catches exact-mode delivery silently choosing an arbitrary audio format."""
    result = validate_candidate(
        request_fixture(audio_format="m4a"), candidate_fixture(audio_formats=())
    )

    assert not result.usable
    assert CandidateRejectionReason.AUDIO_FORMAT_UNAVAILABLE in result.reasons


def test_exact_incomplete_album_is_rejected_but_fast_mode_can_offer_it():
    """Catches an incomplete album silently winning an exact request."""
    candidate = candidate_fixture(
        kind=MediaKind.ALBUM,
        items=(MediaItem("abc123:0", MediaKind.PHOTO, "https://cdn/1.jpg"),),
        complete=False,
        has_video=False,
        has_audio=False,
    )

    exact = validate_candidate(request_fixture(kind=MediaKind.ALBUM), candidate)
    fast = validate_candidate(
        request_fixture(kind=MediaKind.ALBUM, exact=False), candidate
    )

    assert CandidateRejectionReason.ALBUM_INCOMPLETE in exact.reasons
    assert fast.usable


def test_exact_no_watermark_request_rejects_watermarked_candidate_only():
    """Catches exact no-watermark policy being ignored while preserving fast mode."""
    candidate = candidate_fixture(
        kind=MediaKind.VIDEO,
        watermark_free=False,
    )

    exact = validate_candidate(
        request_fixture(watermark_allowed=False), candidate
    )
    fast = validate_candidate(
        request_fixture(watermark_allowed=False, exact=False), candidate
    )

    assert CandidateRejectionReason.WATERMARK_PRESENT in exact.reasons
    assert fast.usable


def test_explicit_media_kind_mismatch_is_rejected_in_exact_and_fast_modes():
    """Catches photo/audio/video requests accepting another explicit media kind."""
    mismatches = (
        (MediaKind.PHOTO, MediaKind.VIDEO),
        (MediaKind.AUDIO, MediaKind.VIDEO),
        (MediaKind.VIDEO, MediaKind.PHOTO),
    )
    for requested, offered in mismatches:
        for exact in (True, False):
            result = validate_candidate(
                request_fixture(kind=requested, exact=exact),
                candidate_fixture(kind=offered),
            )
            assert CandidateRejectionReason.KIND_MISMATCH in result.reasons


def test_legacy_candidate_without_explicit_kind_remains_video_compatible():
    """Protects providers created before explicit kind metadata was introduced."""
    assert validate_candidate(
        request_fixture(kind=MediaKind.VIDEO), candidate_fixture(kind=None)
    ).usable
