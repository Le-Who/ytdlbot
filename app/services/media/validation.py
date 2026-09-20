"""Candidate selection validation for the immutable media contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import MediaCandidate, MediaKind, MediaRequest


class CandidateRejectionReason(StrEnum):
    MEDIA_ID_MISMATCH = "media_id_mismatch"
    VIDEO_UNAVAILABLE = "video_unavailable"
    KIND_MISMATCH = "kind_mismatch"
    ALBUM_INCOMPLETE = "album_incomplete"
    ALBUM_SELECTION_UNAVAILABLE = "album_selection_unavailable"
    WATERMARK_PRESENT = "watermark_present"
    WATERMARK_UNKNOWN = "watermark_unknown"
    QUALITY_TOO_LOW = "quality_too_low"
    AUDIO_UNAVAILABLE = "audio_unavailable"
    AUDIO_FORMAT_UNAVAILABLE = "audio_format_unavailable"
    AUDIO_LANGUAGE_UNAVAILABLE = "audio_language_unavailable"
    AUTH_SCOPE_MISMATCH = "auth_scope_mismatch"


@dataclass(frozen=True, slots=True)
class CandidateValidationResult:
    usable: bool
    reasons: tuple[CandidateRejectionReason, ...] = ()


def validate_candidate(
    request: MediaRequest, candidate: MediaCandidate
) -> CandidateValidationResult:
    reasons: list[CandidateRejectionReason] = []

    if candidate.media_id is not None and candidate.media_id != request.media_id:
        reasons.append(CandidateRejectionReason.MEDIA_ID_MISMATCH)

    if candidate.auth_scope != request.auth_scope:
        reasons.append(CandidateRejectionReason.AUTH_SCOPE_MISMATCH)

    if not _kind_is_compatible(request, candidate):
        reasons.append(CandidateRejectionReason.KIND_MISMATCH)

    if (
        request.exact
        and not candidate.complete
        and (request.kind is MediaKind.ALBUM or candidate.kind is MediaKind.ALBUM)
    ):
        reasons.append(CandidateRejectionReason.ALBUM_INCOMPLETE)

    if request.album_selection:
        if not candidate.items:
            if request.album_selection != (0,):
                reasons.append(CandidateRejectionReason.ALBUM_SELECTION_UNAVAILABLE)
        elif any(
            index < 0 or index >= len(candidate.items)
            for index in request.album_selection
        ):
            reasons.append(CandidateRejectionReason.ALBUM_SELECTION_UNAVAILABLE)

    if request.exact and not request.watermark_allowed:
        if candidate.watermark_free is False:
            reasons.append(CandidateRejectionReason.WATERMARK_PRESENT)
        elif candidate.watermark_free is None:
            reasons.append(CandidateRejectionReason.WATERMARK_UNKNOWN)

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
    elif (
        request.audio_language
        and request.audio_language not in candidate.audio_languages
    ):
        reasons.append(CandidateRejectionReason.AUDIO_LANGUAGE_UNAVAILABLE)

    return CandidateValidationResult(usable=not reasons, reasons=tuple(reasons))


def _kind_is_compatible(request: MediaRequest, candidate: MediaCandidate) -> bool:
    offered = candidate.kind
    if offered is None:
        return request.kind in {MediaKind.AUTO, MediaKind.VIDEO, MediaKind.AUDIO}
    if request.kind is MediaKind.AUTO:
        return offered in {
            MediaKind.VIDEO,
            MediaKind.PHOTO,
            MediaKind.ANIMATION,
            MediaKind.ALBUM,
        }
    if request.kind is MediaKind.AUDIO:
        return offered in {MediaKind.AUDIO, MediaKind.VIDEO}
    return offered is request.kind
