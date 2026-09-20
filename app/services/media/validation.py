"""Candidate selection validation for the immutable media contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import MediaCandidate, MediaKind, MediaRequest


class CandidateRejectionReason(StrEnum):
    VIDEO_UNAVAILABLE = "video_unavailable"
    QUALITY_TOO_LOW = "quality_too_low"
    AUDIO_UNAVAILABLE = "audio_unavailable"
    AUDIO_FORMAT_UNAVAILABLE = "audio_format_unavailable"
    AUDIO_LANGUAGE_UNAVAILABLE = "audio_language_unavailable"


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    usable: bool
    reasons: tuple[CandidateRejectionReason, ...] = ()


def validate_candidate(
    request: MediaRequest, candidate: MediaCandidate
) -> CandidateValidationResult:
    reasons: list[CandidateRejectionReason] = []

    if request.kind is MediaKind.VIDEO and not candidate.has_video:
        reasons.append(CandidateRejectionReason.VIDEO_UNAVAILABLE)

    if request.quality.max_edge is not None:
        short_edge = min(candidate.width or 0, candidate.height or 0)
        if short_edge < request.quality.max_edge:
            reasons.append(CandidateRejectionReason.QUALITY_TOO_LOW)

    audio_requested = (
        request.kind is MediaKind.AUDIO
        or request.audio_format is not None
        or request.audio_language is not None
    )
    if audio_requested and not candidate.has_audio:
        reasons.append(CandidateRejectionReason.AUDIO_UNAVAILABLE)
    elif request.audio_format and request.audio_format not in candidate.audio_formats:
        reasons.append(CandidateRejectionReason.AUDIO_FORMAT_UNAVAILABLE)
    elif request.audio_language and request.audio_language not in candidate.audio_languages:
        reasons.append(CandidateRejectionReason.AUDIO_LANGUAGE_UNAVAILABLE)

    return CandidateValidationResult(usable=not reasons, reasons=tuple(reasons))
