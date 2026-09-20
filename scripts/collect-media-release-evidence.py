#!/usr/bin/env python3
"""Collect redacted production media evidence through a trusted command adapter.

The collector deliberately does not infer per-request data from process-wide metrics.
Its adapter must return an unambiguous one-request delta and explicitly confirm
case-scoped cold-cache eviction. An atomic pre-submit reservation makes every
unknown outcome fail closed until audited reconciliation. Raw URLs and credentials
exist only in adapter input and are never written to evidence files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator, FormatChecker

PHASES = ("resolve", "first_byte", "materialize", "deliver")
CACHE_STATES = ("cold", "warm")
WINDOW_IDS = ("window-1", "window-2", "window-3")
HTTP_CAUSES = (
    "rate-limited",
    "forbidden",
    "expired-signature",
    "geo-blocked",
    "bot-detection",
    "upstream-policy",
    "unknown",
)
CORRELATED_EVENTS = {
    "webhook-accepted",
    "job-completed",
    "job-failed",
    "delivery-success",
    "delivery-failed",
    "delivery-uncertain",
}
SUCCESS_RESULTS = {"success", "delivered"}
RESULT_LABELS = SUCCESS_RESULTS | {"failed", "error", "uncertain", "partial"}
FAILURE_STAGES = {
    "resolve",
    "first_byte",
    "materialize",
    "deliver",
    "validation",
    "cancelled",
}
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
CANDIDATE_IMAGE_REFERENCE_RE = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")
IMAGE_BINDINGS = {"legacy-deployment-attestation", "candidate-release-and-digest"}
FIRST_BYTE_METHODS = {
    "task11-metric",
    "legacy-correlated-progress",
    "file-id-cache",
    "unavailable",
}
DOWNLOADED_BYTES_METHODS = {
    "structured-request-bytes",
    "legacy-correlated-delivered-size",
    "legacy-isolated-delivered-size",
    "file-id-cache",
    "unavailable",
}


class EvidenceError(RuntimeError):
    """Base error whose message is safe to display to an operator."""


class EvidenceValidationError(EvidenceError):
    """Input, resume state, or finalized evidence is inconsistent."""


class UnsafeColdCacheError(EvidenceError):
    """The adapter could not prove exact case-scoped cache/artifact eviction."""


class ObservationError(EvidenceError):
    """A bounded run did not yield attributable, sanitized observations."""


@dataclass(frozen=True, slots=True)
class CaseSpec:
    case_id: str
    kind: str
    url: str


class EvidenceAdapter(Protocol):
    """Trusted boundary around secrets, the bot container, and host telemetry."""

    def identity(self) -> dict[str, Any]: ...

    def evict_case(self, case: CaseSpec) -> bool: ...

    def observe(
        self,
        *,
        update: dict[str, Any],
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...

    def observe_isolated(
        self,
        *,
        case: CaseSpec,
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...

    def cancel(self, correlation_id: str) -> bool: ...


class JsonCommandAdapter:
    """Invoke a reviewed host adapter using JSON over stdin, never command args."""

    def __init__(self, command: Sequence[str]) -> None:
        if not command:
            raise EvidenceValidationError("adapter command is required")
        self._command = tuple(command)

    def _invoke(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [*self._command, action],
                input=json.dumps(payload, separators=(",", ":")),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"adapter {action} exceeded its deadline") from exc
        except OSError as exc:
            raise ObservationError(f"adapter {action} could not be started") from exc
        if result.returncode != 0:
            # Adapter output may contain URLs or credentials. Never include it here.
            raise ObservationError(
                f"adapter {action} failed with exit {result.returncode}"
            )
        try:
            decoded = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ObservationError(f"adapter {action} returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ObservationError(f"adapter {action} must return a JSON object")
        return cast(dict[str, Any], decoded)

    def identity(self) -> dict[str, Any]:
        return self._invoke("identity", {}, timeout_seconds=15.0)

    def evict_case(self, case: CaseSpec) -> bool:
        result = self._invoke(
            "evict-case",
            {"case_id": case.case_id, "kind": case.kind, "url": case.url},
            timeout_seconds=30.0,
        )
        return result == {"safe": True}

    def observe(
        self,
        *,
        update: dict[str, Any],
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        return self._invoke(
            "observe",
            {
                "update": update,
                "correlation_id": correlation_id,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds + 5.0,
        )

    def observe_isolated(
        self,
        *,
        case: CaseSpec,
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        result = self._invoke(
            "observe-isolated",
            {
                "case": {
                    "case_id": case.case_id,
                    "kind": case.kind,
                    "url": case.url,
                },
                "correlation_id": correlation_id,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds + 20.0,
        )
        observation = result.get("observation")
        if not isinstance(observation, dict):
            raise ObservationError("adapter isolated run returned invalid observation")
        return cast(dict[str, Any], observation)

    def cancel(self, correlation_id: str) -> bool:
        result = self._invoke(
            "cancel",
            {"correlation_id": correlation_id},
            timeout_seconds=15.0,
        )
        return result == {"cancelled": True}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"invalid JSON file: {path.name}") from exc
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"JSON root must be an object: {path.name}")
    return cast(dict[str, Any], payload)


def _validate_schema(instance: dict[str, Any], schema_path: Path) -> None:
    schema = _load_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "root"
        raise EvidenceValidationError(
            f"schema validation failed at {location} ({first.validator})"
        )


def _manifest_cases(manifest_path: Path) -> tuple[list[CaseSpec], str]:
    raw = manifest_path.read_bytes()
    manifest = _load_json(manifest_path)
    schema_path = manifest_path.with_name("youtube-acceptance.schema.json")
    _validate_schema(manifest, schema_path)
    approval = manifest.get("approval", {})
    if approval.get("status") != "approved":
        raise EvidenceValidationError("the live manifest is not approved")
    cases = [
        CaseSpec(case_id=item["case_id"], kind=kind, url=item["url"])
        for kind, bucket in (("short", "shorts"), ("video", "videos"))
        for item in manifest[bucket]
    ]
    expected = {
        *(f"short-{index:02d}" for index in range(1, 13)),
        *(f"video-{index:02d}" for index in range(1, 13)),
    }
    if len(cases) != 24 or {case.case_id for case in cases} != expected:
        raise EvidenceValidationError(
            "manifest must contain the approved 12+12 case set"
        )
    if len({case.url for case in cases}) != 24:
        raise EvidenceValidationError("manifest URLs must be unique")
    return cases, hashlib.sha256(raw).hexdigest()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceValidationError(f"{field} must be a positive integer")
    return value


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ObservationError(f"{field} must be finite and nonnegative")
    return converted


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationError(f"{field} must be a nonnegative integer")
    return value


def _update_id(correlation_id: str) -> int:
    digest = hashlib.sha256(correlation_id.encode("utf-8")).digest()
    return 1_000_000_000 + int.from_bytes(digest[:4], "big") % 1_000_000_000


def build_webhook_update(
    *,
    case: CaseSpec,
    correlation_id: str,
    unix_time: int,
) -> dict[str, Any]:
    """Build the exact private-chat /mp4 command consumed by the bot router."""
    text = f"/mp4 {case.url}"
    return {
        "update_id": _update_id(correlation_id),
        "message": {
            "message_id": _update_id(f"message:{correlation_id}"),
            "date": unix_time,
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
        },
    }


def _safe_http_failures(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ObservationError("http_failures must be a list")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"status", "cause"}:
            raise ObservationError("http failure must contain only status and cause")
        if item["status"] not in (403, 429) or item["cause"] not in HTTP_CAUSES:
            raise ObservationError("http failure contains an unsafe value")
        result.append({"status": item["status"], "cause": item["cause"]})
    return result


def _safe_route(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "capability",
        "provenance",
        "attempted",
        "succeeded",
        "route_class",
    }:
        raise ObservationError("independent_route has an invalid shape")
    capability = raw["capability"]
    provenance = raw["provenance"]
    attempted = raw["attempted"]
    succeeded = raw["succeeded"]
    route_class = raw["route_class"]
    if capability == "unavailable":
        if provenance != "unavailable" or any(
            value is not None for value in (attempted, succeeded, route_class)
        ):
            raise ObservationError("unavailable route evidence must have null outcomes")
        return dict(raw)
    if capability != "available" or provenance not in {
        "exact-metric",
        "correlated-event",
    }:
        raise ObservationError("independent route capability is invalid")
    if not isinstance(attempted, bool) or not isinstance(succeeded, bool):
        raise ObservationError("independent route flags must be boolean")
    if not attempted and (succeeded or route_class is not None):
        raise ObservationError("unattempted independent route cannot have an outcome")
    if attempted and route_class not in {"external-free", "external-configured"}:
        raise ObservationError("attempted independent route needs a safe route class")
    if succeeded and not attempted:
        raise ObservationError("successful independent route must be attempted")
    return {
        "capability": capability,
        "provenance": provenance,
        "attempted": attempted,
        "succeeded": succeeded,
        "route_class": route_class,
    }


def _result_delta(before: Any, after: Any) -> tuple[int, int]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ObservationError("pipeline result snapshots must be objects")
    keys = set(before) | set(after)
    if any(not isinstance(key, str) or key not in RESULT_LABELS for key in keys):
        raise ObservationError("pipeline result snapshots contain an unsafe label")
    success = 0
    total = 0
    for key in keys:
        before_value = _nonnegative_int(before.get(key, 0), f"result.{key}.before")
        after_value = _nonnegative_int(after.get(key, 0), f"result.{key}.after")
        delta = after_value - before_value
        if delta < 0:
            raise ObservationError("pipeline result counter decreased")
        total += delta
        if key in SUCCESS_RESULTS:
            success += delta
    if total != 1:
        raise ObservationError("concurrent activity made pipeline results ambiguous")
    return success, total


def _phase_deltas(
    raw: Any,
    *,
    successful: bool,
    bypassed: set[str],
    unavailable: set[str],
) -> dict[str, float | None]:
    if not isinstance(raw, dict) or set(raw) != set(PHASES):
        raise ObservationError("phase_metrics must contain the four pipeline phases")
    result: dict[str, float | None] = {}
    for phase in PHASES:
        item = raw[phase]
        required = {"before_count", "before_sum", "after_count", "after_sum"}
        if not isinstance(item, dict) or set(item) != required:
            raise ObservationError(f"phase metric {phase} has an invalid shape")
        before_count = _nonnegative_int(item["before_count"], f"{phase}.before_count")
        after_count = _nonnegative_int(item["after_count"], f"{phase}.after_count")
        count_delta = after_count - before_count
        expected_count = 0 if phase in bypassed else 1
        before_sum = _nonnegative_number(item["before_sum"], f"{phase}.before_sum")
        after_sum = _nonnegative_number(item["after_sum"], f"{phase}.after_sum")
        sum_delta = after_sum - before_sum
        if sum_delta < -1e-9:
            raise ObservationError(f"phase metric {phase} decreased")
        if phase in unavailable:
            if count_delta < 0:
                raise ObservationError("phase metric counter decreased")
            result[phase] = None
            continue
        if (
            count_delta < 0
            or count_delta > 1
            or (successful and count_delta != expected_count)
        ):
            raise ObservationError("concurrent activity made phase metrics ambiguous")
        if successful and phase in bypassed:
            if abs(sum_delta) > 1e-9:
                raise ObservationError(f"bypassed phase metric {phase} changed")
            result[phase] = 0.0
        else:
            result[phase] = max(0.0, sum_delta) if count_delta == 1 else None
    return result


def _measurement(raw: Any) -> tuple[dict[str, Any], int | None]:
    if not isinstance(raw, dict) or set(raw) != {"first_byte", "downloaded_bytes"}:
        raise ObservationError("measurement metadata has an invalid shape")
    first_byte = raw["first_byte"]
    downloaded = raw["downloaded_bytes"]
    if not isinstance(first_byte, dict) or set(first_byte) != {
        "availability",
        "method",
    }:
        raise ObservationError("first-byte measurement has an invalid shape")
    if not isinstance(downloaded, dict) or set(downloaded) != {
        "availability",
        "method",
        "value",
    }:
        raise ObservationError("downloaded-byte measurement has an invalid shape")
    for item, methods, label in (
        (first_byte, FIRST_BYTE_METHODS, "first-byte"),
        (downloaded, DOWNLOADED_BYTES_METHODS, "downloaded-byte"),
    ):
        availability = item.get("availability")
        method = item.get("method")
        if availability not in {"measured", "unavailable"} or method not in methods:
            raise ObservationError(f"{label} measurement is invalid")
        if (availability == "unavailable") != (method == "unavailable"):
            raise ObservationError(f"{label} availability and method disagree")
    value = downloaded["value"]
    if downloaded["availability"] == "unavailable":
        if value is not None:
            raise ObservationError("unavailable downloaded bytes must be null")
        bytes_downloaded = None
    else:
        bytes_downloaded = _nonnegative_int(value, "downloaded_bytes.value")
    return {
        "first_byte": dict(first_byte),
        "downloaded_bytes": {
            "availability": downloaded["availability"],
            "method": downloaded["method"],
        },
    }, bytes_downloaded


def _run_from_observation(
    *,
    case: CaseSpec,
    cache_state: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    if observation.get("attribution_confirmed") is not True:
        raise ObservationError(
            "adapter could not confirm per-case observation attribution"
        )
    events = observation.get("correlated_events")
    if (
        not isinstance(events, list)
        or not events
        or any(
            not isinstance(item, str) or item not in CORRELATED_EVENTS
            for item in events
        )
    ):
        raise ObservationError("correlated logs contain missing or unsafe events")
    success_delta, _ = _result_delta(
        observation.get("pipeline_results_before"),
        observation.get("pipeline_results_after"),
    )
    delivery_outcomes = observation.get("delivery_outcomes")
    if not isinstance(delivery_outcomes, list) or not delivery_outcomes:
        raise ObservationError("delivery outcomes are required")
    if any(
        item not in {"success", "delivered", "failed", "uncertain"}
        for item in delivery_outcomes
    ):
        raise ObservationError("delivery outcomes contain an unsafe value")
    full_delivery = (
        success_delta == 1
        and observation.get("job_state") == "completed"
        and all(item in SUCCESS_RESULTS for item in delivery_outcomes)
    )
    raw_bypassed = observation.get("bypassed_phases", [])
    if (
        not isinstance(raw_bypassed, list)
        or any(
            item not in {"resolve", "first_byte", "materialize"}
            for item in raw_bypassed
        )
        or len(set(raw_bypassed)) != len(raw_bypassed)
    ):
        raise ObservationError("bypassed_phases contains an unsafe value")
    bypassed = set(raw_bypassed)
    raw_unavailable = observation.get("unavailable_phases", [])
    if (
        not isinstance(raw_unavailable, list)
        or any(item not in PHASES for item in raw_unavailable)
        or len(set(raw_unavailable)) != len(raw_unavailable)
    ):
        raise ObservationError("unavailable_phases contains an unsafe value")
    unavailable = set(raw_unavailable)
    if unavailable & bypassed:
        raise ObservationError("a phase cannot be both bypassed and unavailable")
    if bypassed:
        before_hits = _nonnegative_int(
            observation.get("file_id_hits_before"), "file_id_hits_before"
        )
        after_hits = _nonnegative_int(
            observation.get("file_id_hits_after"), "file_id_hits_after"
        )
        if after_hits - before_hits != 1:
            raise ObservationError("bypassed phases require one file_id cache hit")
    latencies = _phase_deltas(
        observation.get("phase_metrics"),
        successful=full_delivery,
        bypassed=bypassed,
        unavailable=unavailable,
    )
    measurement, bytes_downloaded = _measurement(observation.get("measurement"))
    first_byte_available = measurement["first_byte"]["availability"] == "measured"
    if first_byte_available != ("first_byte" not in unavailable):
        raise ObservationError("first-byte phase and measurement availability disagree")
    failure_stage: str | None = None
    if not full_delivery:
        supplied_stage = observation.get("failure_stage")
        if supplied_stage is not None and supplied_stage not in FAILURE_STAGES:
            raise ObservationError("failure_stage contains an unsafe value")
        failure_stage = supplied_stage or next(
            (phase for phase in PHASES if latencies[phase] is None), "deliver"
        )
    before_waste = _nonnegative_int(
        observation.get("wasted_bytes_before"), "wasted_bytes_before"
    )
    after_waste = _nonnegative_int(
        observation.get("wasted_bytes_after"), "wasted_bytes_after"
    )
    if after_waste < before_waste:
        raise ObservationError("wasted-byte counter decreased")
    cpu_before = _nonnegative_number(
        observation.get("cpu_seconds_before"), "cpu_seconds_before"
    )
    cpu_after = _nonnegative_number(
        observation.get("cpu_seconds_after"), "cpu_seconds_after"
    )
    if cpu_after < cpu_before:
        raise ObservationError("container CPU counter decreased")
    rss_samples = observation.get("rss_bytes_samples")
    if not isinstance(rss_samples, list) or not rss_samples:
        raise ObservationError("container RSS samples are required")
    rss_values = [_nonnegative_int(value, "rss_bytes_samples") for value in rss_samples]
    return {
        "case_id": case.case_id,
        "kind": case.kind,
        "cache_state": cache_state,
        "latency_seconds": latencies,
        "measurement": measurement,
        "full_delivery": full_delivery,
        "failure_stage": failure_stage,
        "http_failures": _safe_http_failures(observation.get("http_failures")),
        "bytes_downloaded": bytes_downloaded,
        "bytes_wasted": after_waste - before_waste,
        "process_cpu_seconds": cpu_after - cpu_before,
        "peak_rss_bytes": max(rss_values),
        "independent_route": _safe_route(observation.get("independent_route")),
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _resume_state(
    *,
    output_path: Path,
    window_id: str,
    release_sha: str,
    manifest_sha256: str,
    collected_at: str,
    memory_limit: int,
    production_ip_attested: bool,
    runtime_profile: str,
    image_id: str,
    image_reference: str,
    identity_binding: str,
    case_kinds: dict[str, str],
) -> dict[str, Any]:
    if not output_path.exists():
        return {
            "schema_version": 1,
            "release_sha": release_sha,
            "manifest_sha256": manifest_sha256,
            "window_id": window_id,
            "started_at": collected_at,
            "current_memory_limit_bytes": memory_limit,
            "production_ip_attested": production_ip_attested,
            "runtime_profile": runtime_profile,
            "image_id": image_id,
            "image_reference": image_reference,
            "identity_binding": identity_binding,
            "in_flight": None,
            "reconciliations": [],
            "runs": [],
        }
    payload = _load_json(output_path)
    metadata = {
        "schema_version": 1,
        "release_sha": release_sha,
        "manifest_sha256": manifest_sha256,
        "window_id": window_id,
        "current_memory_limit_bytes": memory_limit,
        "production_ip_attested": production_ip_attested,
        "runtime_profile": runtime_profile,
        "image_id": image_id,
        "image_reference": image_reference,
        "identity_binding": identity_binding,
    }
    if any(payload.get(key) != value for key, value in metadata.items()):
        raise EvidenceValidationError("resume metadata does not match this collection")
    if (
        not isinstance(payload.get("started_at"), str)
        or not isinstance(payload.get("runs"), list)
        or not isinstance(payload.get("reconciliations"), list)
        or "in_flight" not in payload
    ):
        raise EvidenceValidationError("resume file has an invalid shape")
    if payload["in_flight"] is not None:
        raise EvidenceValidationError(
            "window has an unresolved IN_FLIGHT reservation; audited reconciliation required"
        )
    seen: set[tuple[str, str]] = set()
    for run in payload["runs"]:
        if not isinstance(run, dict):
            raise EvidenceValidationError("resume runs must be objects")
        key = (run.get("case_id"), run.get("cache_state"))
        if key in seen:
            raise EvidenceValidationError("resume file contains duplicate runs")
        if key[0] not in case_kinds or key[1] not in CACHE_STATES:
            raise EvidenceValidationError("resume file contains an unknown run")
        if run.get("kind") != case_kinds[key[0]]:
            raise EvidenceValidationError("resume run kind does not match the manifest")
        seen.add(cast(tuple[str, str], key))
    return payload


def reconcile_in_flight(
    *,
    output_path: Path,
    decision: str,
    audited_by: str,
    reconciled_at: str,
) -> None:
    """Clear one unknown reservation only after an explicit operator audit."""
    if decision != "confirmed-not-accepted":
        raise EvidenceValidationError("unsupported reconciliation decision")
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@"
    if not audited_by or any(character not in safe for character in audited_by):
        raise EvidenceValidationError("audited_by contains unsafe characters")
    _unix_time(reconciled_at)
    state = _load_json(output_path)
    reservation = state.get("in_flight")
    if not isinstance(reservation, dict) or reservation.get("state") != "IN_FLIGHT":
        raise EvidenceValidationError("no IN_FLIGHT reservation to reconcile")
    state.setdefault("reconciliations", []).append(
        {
            "case_id": reservation.get("case_id"),
            "cache_state": reservation.get("cache_state"),
            "correlation_id": reservation.get("correlation_id"),
            "update_id": reservation.get("update_id"),
            "decision": decision,
            "audited_by": audited_by,
            "reconciled_at": reconciled_at,
        }
    )
    state["in_flight"] = None
    _atomic_json(output_path, state)


def _unix_time(timestamp: str) -> int:
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError as exc:
        raise EvidenceValidationError(
            "collected_at must be an ISO 8601 timestamp"
        ) from exc


def collect_window(
    *,
    manifest_path: Path,
    output_path: Path,
    window_id: str,
    release_sha: str,
    adapter: EvidenceAdapter,
    correlation_prefix: str,
    timeout_seconds: float,
    collected_at: str,
    plan: str = "full",
) -> None:
    """Collect or resume one fixed window, atomically saving after every run."""
    if window_id not in WINDOW_IDS:
        raise EvidenceValidationError("window_id must be window-1..3")
    if len(release_sha) != 40 or any(
        character not in "0123456789abcdef" for character in release_sha
    ):
        raise EvidenceValidationError(
            "release_sha must be a lowercase 40-character SHA"
        )
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise EvidenceValidationError("timeout_seconds must be positive")
    if plan not in {"full", "smoke"}:
        raise EvidenceValidationError("plan must be full or smoke")
    if not correlation_prefix or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        for character in correlation_prefix
    ):
        raise EvidenceValidationError("correlation_prefix contains unsafe characters")
    cases, manifest_sha256 = _manifest_cases(manifest_path)
    identity = adapter.identity()
    memory_limit = _positive_int(
        identity.get("current_memory_limit_bytes"), "current_memory_limit_bytes"
    )
    if identity.get("production_ip_attested") is not True:
        raise EvidenceValidationError(
            "adapter did not attest the production-IP boundary"
        )
    if identity.get("release") != release_sha:
        raise EvidenceValidationError(
            "adapter release identity does not match collection"
        )
    runtime_profile = identity.get("runtime_profile")
    if runtime_profile not in {"legacy-baseline", "candidate"}:
        raise EvidenceValidationError("adapter runtime profile is invalid")
    image_id = identity.get("image_id")
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        raise EvidenceValidationError("adapter image identity is invalid")
    image_reference = identity.get("image_reference")
    if not isinstance(image_reference, str) or not image_reference:
        raise EvidenceValidationError("adapter image reference is invalid")
    identity_binding = identity.get("identity_binding")
    if identity_binding not in IMAGE_BINDINGS:
        raise EvidenceValidationError("adapter identity binding is invalid")
    if runtime_profile == "candidate" and (
        identity_binding != "candidate-release-and-digest"
        or not CANDIDATE_IMAGE_REFERENCE_RE.fullmatch(image_reference)
    ):
        raise EvidenceValidationError("candidate image binding is invalid")
    if runtime_profile == "legacy-baseline" and (
        identity_binding != "legacy-deployment-attestation"
        or image_reference != image_id
    ):
        raise EvidenceValidationError("legacy image binding is invalid")
    case_kinds = {case.case_id: case.kind for case in cases}
    state = _resume_state(
        output_path=output_path,
        window_id=window_id,
        release_sha=release_sha,
        manifest_sha256=manifest_sha256,
        collected_at=collected_at,
        memory_limit=memory_limit,
        production_ip_attested=True,
        runtime_profile=runtime_profile,
        image_id=image_id,
        image_reference=image_reference,
        identity_binding=identity_binding,
        case_kinds=case_kinds,
    )
    existing_plan = state.get("plan")
    if existing_plan not in {None, plan}:
        raise EvidenceValidationError("resume plan does not match this collection")
    state["plan"] = plan
    completed = {(run["case_id"], run["cache_state"]) for run in state["runs"]}
    unix_time = _unix_time(state["started_at"])
    if runtime_profile == "legacy-baseline":
        if plan != "smoke":
            raise EvidenceValidationError(
                "legacy baseline supports only the bounded smoke plan"
            )
        case = next(item for item in cases if item.kind == "short")
        key = (case.case_id, "cold")
        if key in completed:
            return
        correlation_id = f"{correlation_prefix}:{window_id}:{case.case_id}:cold"
        update_id = _update_id(correlation_id)
        state["in_flight"] = {
            "case_id": case.case_id,
            "kind": case.kind,
            "cache_state": "cold",
            "correlation_id": correlation_id,
            "update_id": update_id,
            "reserved_at": state["started_at"],
            "state": "IN_FLIGHT",
        }
        _atomic_json(output_path, state)
        try:
            observation = adapter.observe_isolated(
                case=case,
                correlation_id=correlation_id,
                timeout_seconds=timeout_seconds,
            )
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            cancelled = adapter.cancel(correlation_id)
            if not cancelled:
                raise ObservationError(
                    f"timeout cancellation could not be confirmed for {case.case_id}; "
                    "IN_FLIGHT reconciliation required"
                ) from exc
            raise ObservationError(
                f"{case.case_id} isolated smoke cancelled after timeout; "
                "IN_FLIGHT reconciliation required"
            ) from exc
        state["runs"].append(
            _run_from_observation(
                case=case,
                cache_state="cold",
                observation=observation,
            )
        )
        state["in_flight"] = None
        _atomic_json(output_path, state)
        return
    if plan != "full":
        raise EvidenceValidationError("candidate smoke plan is not implemented")
    for case in cases:
        for cache_state in CACHE_STATES:
            key = (case.case_id, cache_state)
            if key in completed:
                continue
            if cache_state == "cold" and not adapter.evict_case(case):
                raise UnsafeColdCacheError(
                    f"exact case-scoped cache/artifact eviction unavailable for {case.case_id}"
                )
            correlation_id = (
                f"{correlation_prefix}:{window_id}:{case.case_id}:{cache_state}"
            )
            update = build_webhook_update(
                case=case,
                correlation_id=correlation_id,
                unix_time=unix_time,
            )
            state["in_flight"] = {
                "case_id": case.case_id,
                "kind": case.kind,
                "cache_state": cache_state,
                "correlation_id": correlation_id,
                "update_id": update["update_id"],
                "reserved_at": state["started_at"],
                "state": "IN_FLIGHT",
            }
            _atomic_json(output_path, state)
            try:
                observation = adapter.observe(
                    update=update,
                    correlation_id=correlation_id,
                    timeout_seconds=timeout_seconds,
                )
            except (TimeoutError, subprocess.TimeoutExpired) as exc:
                cancelled = adapter.cancel(correlation_id)
                if not cancelled:
                    raise ObservationError(
                        f"timeout cancellation could not be confirmed for {case.case_id}; "
                        "IN_FLIGHT reconciliation required"
                    ) from exc
                raise ObservationError(
                    f"{case.case_id} {cache_state} cancelled after timeout; "
                    "IN_FLIGHT reconciliation required"
                ) from exc
            run = _run_from_observation(
                case=case,
                cache_state=cache_state,
                observation=observation,
            )
            state["runs"].append(run)
            state["in_flight"] = None
            completed.add(key)
            _atomic_json(output_path, state)


def finalize_smoke(
    *, window_path: Path, output_path: Path, collected_at: str
) -> None:
    """Finalize one representative delivery without statistical claims."""
    _unix_time(collected_at)
    state = _load_json(window_path)
    if state.get("plan") != "smoke" or state.get("runtime_profile") != "legacy-baseline":
        raise EvidenceValidationError("smoke report requires a legacy smoke window")
    if state.get("in_flight") is not None:
        raise EvidenceValidationError("cannot finalize an IN_FLIGHT smoke run")
    runs = state.get("runs")
    if (
        not isinstance(runs, list)
        or len(runs) != 1
        or runs[0].get("kind") != "short"
        or runs[0].get("cache_state") != "cold"
    ):
        raise EvidenceValidationError("smoke report requires exactly one short run")
    smoke_accepted = runs[0].get("full_delivery") is True
    report = {
        "schema_version": 1,
        "plan": "smoke",
        "collected_at": collected_at,
        "release_sha": state.get("release_sha"),
        "manifest_sha256": state.get("manifest_sha256"),
        "runtime_profile": state.get("runtime_profile"),
        "image_id": state.get("image_id"),
        "image_reference": state.get("image_reference"),
        "identity_binding": state.get("identity_binding"),
        "current_memory_limit_bytes": state.get("current_memory_limit_bytes"),
        "production_ip_attested": state.get("production_ip_attested"),
        "run": runs[0],
        "acceptance": {
            "latency_p50_p95": {
                "measurement": "NOT_MEASURED",
                "accepted": False,
            },
            "candidate_improvement_25_percent": {
                "measurement": "NOT_MEASURED",
                "accepted": False,
            },
            "statistical_success_rate": {
                "measurement": "NOT_MEASURED",
                "accepted": False,
            },
            "representative_delivery_smoke": {
                "measurement": "MEASURED",
                "accepted": smoke_accepted,
            },
        },
        "redaction": {
            "urls_hashed": True,
            "tokens_removed": True,
            "signed_queries_removed": True,
            "cookies_removed": True,
        },
    }
    _atomic_json(output_path, report)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _summary(
    runs: list[dict[str, Any]], *, kind: str, cache_state: str, memory_limit: int
) -> dict[str, Any]:
    cohort = [
        run for run in runs if run["kind"] == kind and run["cache_state"] == cache_state
    ]
    latencies = {
        phase: {
            "p50": _nearest_rank(
                [
                    float(run["latency_seconds"][phase])
                    for run in cohort
                    if run["latency_seconds"][phase] is not None
                ],
                0.50,
            ),
            "p95": _nearest_rank(
                [
                    float(run["latency_seconds"][phase])
                    for run in cohort
                    if run["latency_seconds"][phase] is not None
                ],
                0.95,
            ),
        }
        for phase in PHASES
    }
    cause_counts = {
        status: Counter(
            failure["cause"]
            for run in cohort
            for failure in run["http_failures"]
            if failure["status"] == status
        )
        for status in (403, 429)
    }
    rss_values = [run["peak_rss_bytes"] for run in cohort]
    attempted = [
        run["independent_route"]
        for run in cohort
        if run["independent_route"]["attempted"] is True
    ]
    peak_max = max(rss_values)
    return {
        "kind": kind,
        "cache_state": cache_state,
        "sample_count": len(cohort),
        "full_delivery_rate": sum(run["full_delivery"] for run in cohort) / len(cohort),
        "latency_seconds": latencies,
        "http_403_causes": {cause: cause_counts[403][cause] for cause in HTTP_CAUSES},
        "http_429_causes": {cause: cause_counts[429][cause] for cause in HTTP_CAUSES},
        "bytes_downloaded": (
            sum(run["bytes_downloaded"] for run in cohort)
            if all(run["bytes_downloaded"] is not None for run in cohort)
            else None
        ),
        "measurement_complete": all(
            run["measurement"]["first_byte"]["availability"] == "measured"
            and run["measurement"]["downloaded_bytes"]["availability"] == "measured"
            for run in cohort
        ),
        "bytes_wasted": sum(run["bytes_wasted"] for run in cohort),
        "process_cpu_seconds": sum(run["process_cpu_seconds"] for run in cohort),
        "peak_rss_bytes": {
            "p50": int(cast(float, _nearest_rank(rss_values, 0.50))),
            "p95": int(cast(float, _nearest_rank(rss_values, 0.95))),
            "max": peak_max,
        },
        "within_current_memory_limit": peak_max <= memory_limit,
        "independent_route_success_rate": (
            sum(route["succeeded"] for route in attempted) / len(attempted)
            if attempted
            else None
        ),
        "independent_route_measurement_complete": all(
            run["independent_route"]["capability"] == "available"
            for run in cohort
        ),
    }


def _validate_final_semantics(
    evidence: dict[str, Any], case_kinds: dict[str, str]
) -> None:
    windows = evidence["windows"]
    if [window["window_id"] for window in windows] != list(WINDOW_IDS):
        raise EvidenceValidationError("evidence must contain window-1..3 in order")
    if len({window["started_at"] for window in windows}) != 3:
        raise EvidenceValidationError("the three windows must have distinct timestamps")
    expected = {
        (case_id, cache_state) for case_id in case_kinds for cache_state in CACHE_STATES
    }
    for window in windows:
        actual = [(run["case_id"], run["cache_state"]) for run in window["runs"]]
        if len(actual) != len(expected) or set(actual) != expected:
            raise EvidenceValidationError(
                "each window must contain each cold/warm case exactly once"
            )
        if any(run["kind"] != case_kinds[run["case_id"]] for run in window["runs"]):
            raise EvidenceValidationError(
                "run kind does not match the approved manifest"
            )


def finalize_evidence(
    *,
    manifest_path: Path,
    schema_path: Path,
    window_paths: Sequence[Path],
    output_path: Path,
    collected_at: str,
) -> None:
    """Merge exactly three completed windows and calculate truthful summaries."""
    cases, manifest_sha256 = _manifest_cases(manifest_path)
    if len(window_paths) != 3:
        raise EvidenceValidationError("finalization requires exactly window-1..3")
    states = [_load_json(path) for path in window_paths]
    by_id = {state.get("window_id"): state for state in states}
    if len(by_id) != 3 or set(by_id) != set(WINDOW_IDS):
        raise EvidenceValidationError("finalization requires exactly window-1..3")
    ordered = [by_id[window_id] for window_id in WINDOW_IDS]
    releases = {state.get("release_sha") for state in ordered}
    manifests = {state.get("manifest_sha256") for state in ordered}
    limits = {state.get("current_memory_limit_bytes") for state in ordered}
    image_ids = {state.get("image_id") for state in ordered}
    image_references = {state.get("image_reference") for state in ordered}
    runtime_profiles = {state.get("runtime_profile") for state in ordered}
    bindings = {state.get("identity_binding") for state in ordered}
    if len(releases) != 1:
        raise EvidenceValidationError("window release SHAs do not match")
    if manifests != {manifest_sha256}:
        raise EvidenceValidationError("window manifest hashes do not match")
    if len(limits) != 1:
        raise EvidenceValidationError("window memory limits do not match")
    if any(
        len(values) != 1
        for values in (image_ids, image_references, runtime_profiles, bindings)
    ):
        raise EvidenceValidationError("window runtime image identities do not match")
    if any(state.get("in_flight") is not None for state in ordered):
        raise EvidenceValidationError("cannot finalize an IN_FLIGHT reservation")
    if any(state.get("production_ip_attested") is not True for state in ordered):
        raise EvidenceValidationError("a window lacks production-IP attestation")
    memory_limit = _positive_int(next(iter(limits)), "current_memory_limit_bytes")
    windows = [
        {
            "window_id": state["window_id"],
            "started_at": state["started_at"],
            "runs": state["runs"],
            "reconciliations": state.get("reconciliations", []),
        }
        for state in ordered
    ]
    all_runs = [run for window in windows for run in window["runs"]]
    summaries = [
        _summary(
            all_runs, kind=kind, cache_state=cache_state, memory_limit=memory_limit
        )
        for kind in ("short", "video")
        for cache_state in CACHE_STATES
    ]
    evidence = {
        "schema_version": 1,
        "release_sha": next(iter(releases)),
        "manifest_sha256": manifest_sha256,
        "collected_at": collected_at,
        "source": "production-vps",
        "production_ip_attested": True,
        "current_memory_limit_bytes": memory_limit,
        "runtime_profile": next(iter(runtime_profiles)),
        "image_id": next(iter(image_ids)),
        "image_reference": next(iter(image_references)),
        "identity_binding": next(iter(bindings)),
        "redaction": {
            "urls_hashed": True,
            "tokens_removed": True,
            "signed_queries_removed": True,
            "cookies_removed": True,
        },
        "windows": windows,
        "summaries": summaries,
    }
    _validate_schema(evidence, schema_path)
    _validate_final_semantics(evidence, {case.case_id: case.kind for case in cases})
    _atomic_json(output_path, evidence)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    collect = subparsers.add_parser("collect-window")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--window", choices=WINDOW_IDS, required=True)
    collect.add_argument("--release-sha", required=True)
    collect.add_argument("--correlation-prefix", required=True)
    collect.add_argument("--timeout-seconds", type=float, default=900.0)
    collect.add_argument("--collected-at", required=True)
    collect.add_argument("--adapter-command", required=True)
    collect.add_argument("--plan", choices=("full", "smoke"), default="full")
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    finalize.add_argument("--schema", type=Path, required=True)
    finalize.add_argument("--window-file", type=Path, action="append", required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--collected-at", required=True)
    finalize_smoke_parser = subparsers.add_parser("finalize-smoke")
    finalize_smoke_parser.add_argument("--window-file", type=Path, required=True)
    finalize_smoke_parser.add_argument("--output", type=Path, required=True)
    finalize_smoke_parser.add_argument("--collected-at", required=True)
    reconcile = subparsers.add_parser("reconcile-in-flight")
    reconcile.add_argument("--output", type=Path, required=True)
    reconcile.add_argument(
        "--decision", choices=("confirmed-not-accepted",), required=True
    )
    reconcile.add_argument("--audited-by", required=True)
    reconcile.add_argument("--reconciled-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "collect-window":
            collect_window(
                manifest_path=args.manifest,
                output_path=args.output,
                window_id=args.window,
                release_sha=args.release_sha,
                adapter=JsonCommandAdapter(shlex.split(args.adapter_command)),
                correlation_prefix=args.correlation_prefix,
                timeout_seconds=args.timeout_seconds,
                collected_at=args.collected_at,
                plan=args.plan,
            )
        elif args.mode == "finalize":
            finalize_evidence(
                manifest_path=args.manifest,
                schema_path=args.schema,
                window_paths=args.window_file,
                output_path=args.output,
                collected_at=args.collected_at,
            )
        elif args.mode == "finalize-smoke":
            finalize_smoke(
                window_path=args.window_file,
                output_path=args.output,
                collected_at=args.collected_at,
            )
        else:
            reconcile_in_flight(
                output_path=args.output,
                decision=args.decision,
                audited_by=args.audited_by,
                reconciled_at=args.reconciled_at,
            )
    except EvidenceError as exc:
        print(f"evidence collection failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
