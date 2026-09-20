from app.services.media import (
    CandidateRejectionReason,
    MediaCandidate,
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
