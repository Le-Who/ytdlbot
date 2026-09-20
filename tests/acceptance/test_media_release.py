from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
YOUTUBE_MANIFEST = FIXTURES / "youtube-acceptance.json"
YOUTUBE_SCHEMA = FIXTURES / "youtube-acceptance.schema.json"
EVIDENCE_SCHEMA = FIXTURES / "media-release-evidence.schema.json"

REQUIRED_TRAITS = {
    "vertical",
    "horizontal",
    "short-duration",
    "long-duration",
    "720p",
    "1080p",
    "separate-av",
    "multiple-audio-tracks",
    "recent-publication",
    "over-50-mb",
}

OFFLINE_ACCEPTANCE_NODES = (
    "tests/media/test_race.py::test_second_valid_provider_wins_without_waiting_for_hung_first",
    "tests/media/test_pipeline.py::test_cached_file_id_is_delivered_before_any_provider_or_transport_work",
    "tests/core/test_media_cache.py::test_file_id_key_isolates_every_output_equivalence_field_and_version",
    "tests/core/test_download_queue_cancellation.py::test_cancelled_waiter_removed_before_grant_and_slot_is_reusable",
    "tests/media/test_delivery.py::test_cloud_limit_uses_decimal_boundary_before_open_or_send",
    "tests/media/test_delivery.py::test_materialized_local_path_returns_confirmed_video_receipt",
    "tests/media/test_delivery.py::test_local_path_is_confined_and_external_path_uses_streaming_multipart",
    "tests/test_compose_contract.py::test_compose_mounts_shared_media_and_separate_durable_state",
    "tests/media/test_race.py::test_caller_cancellation_awaits_provider_cleanup_and_delay_tasks",
    "tests/media/test_transport.py::test_cancellation_closes_stream_and_removes_partial_and_lease",
    "tests/core/test_process_supervisor.py::test_request_cancel_leaves_no_process_socket_or_partial_after_two_seconds",
    "tests/test_health_readiness.py::test_ready_reports_release_limit_store_and_required_local_api",
    "tests/core/test_job_store.py::test_update_id_is_deduplicated_and_payload_keeps_only_telegram_update",
    "tests/core/test_job_store.py::test_recovery_skips_success_and_uncertain_but_retries_failed_delivery",
    "tests/core/test_job_store.py::test_checkpointed_job_is_recovered_after_worker_restart",
    "tests/test_webhook_durability.py::test_delivery_outcome_is_persisted_before_completion_and_not_replayed",
    "tests/test_webhook_durability.py::test_crash_during_telegram_send_is_recovered_as_uncertain_without_replay",
    "tests/deploy/test_release_scripts.py::test_readiness_failure_restores_previous_image_and_config",
    "tests/deploy/test_release_scripts.py::test_interrupted_activation_runs_rollback",
    "tests/deploy/test_release_scripts.py::test_same_sha_promotion_reuses_only_an_identical_immutable_release",
    "tests/test_pipeline_entrypoints.py::test_callback_codec_emits_v2_and_accepts_previous_shape",
)

REQUIRED_OFFLINE_ACCEPTANCE_NODES = {
    "tests/media/test_delivery.py::test_materialized_local_path_returns_confirmed_video_receipt",
    "tests/media/test_delivery.py::test_local_path_is_confined_and_external_path_uses_streaming_multipart",
    "tests/test_compose_contract.py::test_compose_mounts_shared_media_and_separate_durable_state",
    "tests/media/test_race.py::test_caller_cancellation_awaits_provider_cleanup_and_delay_tasks",
    "tests/media/test_transport.py::test_cancellation_closes_stream_and_removes_partial_and_lease",
    "tests/core/test_process_supervisor.py::test_request_cancel_leaves_no_process_socket_or_partial_after_two_seconds",
    "tests/core/test_job_store.py::test_update_id_is_deduplicated_and_payload_keeps_only_telegram_update",
    "tests/core/test_job_store.py::test_recovery_skips_success_and_uncertain_but_retries_failed_delivery",
    "tests/test_webhook_durability.py::test_delivery_outcome_is_persisted_before_completion_and_not_replayed",
    "tests/test_webhook_durability.py::test_crash_during_telegram_send_is_recovered_as_uncertain_without_replay",
    "tests/deploy/test_release_scripts.py::test_readiness_failure_restores_previous_image_and_config",
    "tests/deploy/test_release_scripts.py::test_same_sha_promotion_reuses_only_an_identical_immutable_release",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(instance: dict[str, Any], schema_path: Path) -> None:
    schema = _load_json(schema_path)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate(instance)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _require_approved_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    _validate_schema(manifest, YOUTUBE_SCHEMA)
    assert manifest["approval"]["status"] == "approved", (
        "the 24-link live manifest must be explicitly approved before Task 14"
    )
    assert manifest["approval"]["approved_by"]
    assert manifest["approval"]["approved_at"]
    cases = [*manifest["shorts"], *manifest["videos"]]
    urls = [case["url"] for case in cases]
    assert all(
        isinstance(url, str)
        and re.fullmatch(
            r"https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[A-Za-z0-9_-]+",
            url,
        )
        for url in urls
    )
    assert len(set(urls)) == 24
    expected_shorts = {f"short-{index:02d}" for index in range(1, 13)}
    expected_videos = {f"video-{index:02d}" for index in range(1, 13)}
    assert {case["case_id"] for case in manifest["shorts"]} == expected_shorts
    assert {case["case_id"] for case in manifest["videos"]} == expected_videos
    traits = {trait for case in cases for trait in case["traits"]}
    assert REQUIRED_TRAITS <= traits
    return {
        case["case_id"]: kind
        for kind, bucket in (("short", "shorts"), ("video", "videos"))
        for case in manifest[bucket]
    }


def _validate_evidence(evidence: dict[str, Any], case_kinds: dict[str, str]) -> None:
    _validate_schema(evidence, EVIDENCE_SCHEMA)
    assert re.fullmatch(r"[0-9a-f]{40}", evidence["release_sha"])
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["manifest_sha256"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", evidence["image_id"])
    assert evidence["runtime_profile"] in {"legacy-baseline", "candidate"}
    assert evidence["identity_binding"] in {
        "legacy-deployment-attestation",
        "candidate-release-and-digest",
    }
    if evidence["runtime_profile"] == "candidate":
        assert evidence["identity_binding"] == "candidate-release-and-digest"
        assert re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", evidence["image_reference"])
    else:
        assert evidence["identity_binding"] == "legacy-deployment-attestation"
        assert evidence["image_reference"] == evidence["image_id"]
    assert evidence["source"] == "production-vps"
    assert evidence["production_ip_attested"] is True
    assert evidence["redaction"] == {
        "urls_hashed": True,
        "tokens_removed": True,
        "signed_queries_removed": True,
        "cookies_removed": True,
    }
    memory_limit = evidence["current_memory_limit_bytes"]
    assert isinstance(memory_limit, int) and not isinstance(memory_limit, bool)
    assert memory_limit > 0
    windows = evidence["windows"]
    assert len(windows) == 3
    assert {window["window_id"] for window in windows} == {
        "window-1",
        "window-2",
        "window-3",
    }
    assert len({window["started_at"] for window in windows}) == 3
    expected_runs = {
        (case_id, cache) for case_id in case_kinds for cache in ("cold", "warm")
    }
    for window in windows:
        assert isinstance(window["reconciliations"], list)
        actual_runs = [(run["case_id"], run["cache_state"]) for run in window["runs"]]
        assert len(actual_runs) == len(expected_runs)
        assert set(actual_runs) == expected_runs
        for run in window["runs"]:
            assert run["kind"] == case_kinds[run["case_id"]]
            latencies = run["latency_seconds"]
            assert set(latencies) == {
                "resolve",
                "first_byte",
                "materialize",
                "deliver",
            }
            for latency in latencies.values():
                if latency is None:
                    continue
                assert isinstance(latency, (int, float))
                assert not isinstance(latency, bool)
                assert math.isfinite(latency) and latency >= 0
            assert isinstance(run["full_delivery"], bool)
            if run["full_delivery"]:
                assert all(
                    latencies[phase] is not None
                    for phase in ("resolve", "materialize", "deliver")
                )
            measurement = run["measurement"]
            assert set(measurement) == {"first_byte", "downloaded_bytes"}
            first_available = measurement["first_byte"]["availability"] == "measured"
            assert first_available is (latencies["first_byte"] is not None)
            bytes_available = (
                measurement["downloaded_bytes"]["availability"] == "measured"
            )
            assert bytes_available is (run["bytes_downloaded"] is not None)
            if run["bytes_downloaded"] is not None:
                assert run["bytes_downloaded"] >= 0
            assert run["bytes_wasted"] >= 0
            assert math.isfinite(run["process_cpu_seconds"])
            assert run["process_cpu_seconds"] >= 0
            assert isinstance(run["peak_rss_bytes"], int)
            assert not isinstance(run["peak_rss_bytes"], bool)
            assert run["peak_rss_bytes"] >= 0
            assert all(
                failure["status"] in (403, 429) and failure["cause"]
                for failure in run["http_failures"]
            )
            assert set(run["independent_route"]) == {
                "capability",
                "provenance",
                "attempted",
                "succeeded",
                "route_class",
            }
    summaries = evidence["summaries"]
    expected_summaries = {
        (kind, cache) for kind in ("short", "video") for cache in ("cold", "warm")
    }
    assert len(summaries) == len(expected_summaries)
    assert {(item["kind"], item["cache_state"]) for item in summaries} == (
        expected_summaries
    )
    all_runs = [run for window in windows for run in window["runs"]]
    for summary in summaries:
        assert set(summary["latency_seconds"]) == {
            "resolve",
            "first_byte",
            "materialize",
            "deliver",
        }
        assert all(
            set(percentiles) == {"p50", "p95"}
            for percentiles in summary["latency_seconds"].values()
        )
        cohort = [
            run
            for run in all_runs
            if run["kind"] == summary["kind"]
            and run["cache_state"] == summary["cache_state"]
        ]
        assert summary["sample_count"] == len(cohort)
        assert math.isclose(
            summary["full_delivery_rate"],
            sum(run["full_delivery"] for run in cohort) / len(cohort),
        )
        for status, field in ((403, "http_403_causes"), (429, "http_429_causes")):
            causes = Counter(
                failure["cause"]
                for run in cohort
                for failure in run["http_failures"]
                if failure["status"] == status
            )
            assert summary[field] == {cause: causes[cause] for cause in summary[field]}
        measured_bytes = [run["bytes_downloaded"] for run in cohort]
        expected_downloaded = (
            sum(measured_bytes)
            if all(value is not None for value in measured_bytes)
            else None
        )
        assert summary["bytes_downloaded"] == expected_downloaded
        expected_complete = all(
            run["measurement"]["first_byte"]["availability"] == "measured"
            and run["measurement"]["downloaded_bytes"]["availability"] == "measured"
            for run in cohort
        )
        assert summary["measurement_complete"] is expected_complete
        assert summary["measurement_complete"], (
            "release comparison fails closed when first-byte or downloaded-byte "
            "measurement is unavailable"
        )
        assert summary["bytes_wasted"] == sum(run["bytes_wasted"] for run in cohort)
        assert math.isclose(
            summary["process_cpu_seconds"],
            sum(run["process_cpu_seconds"] for run in cohort),
        )
        assert math.isfinite(summary["process_cpu_seconds"])
        rss_values = [run["peak_rss_bytes"] for run in cohort]
        expected_rss = {
            "p50": _nearest_rank(rss_values, 0.50),
            "p95": _nearest_rank(rss_values, 0.95),
            "max": max(rss_values),
        }
        assert summary["peak_rss_bytes"] == expected_rss
        expected_within_limit = expected_rss["max"] <= memory_limit
        assert summary["within_current_memory_limit"] is expected_within_limit
        assert expected_within_limit
        for stage, percentiles in summary["latency_seconds"].items():
            values = [
                run["latency_seconds"][stage]
                for run in cohort
                if run["latency_seconds"][stage] is not None
            ]
            for label, percentile in (("p50", 0.50), ("p95", 0.95)):
                expected = _nearest_rank(values, percentile)
                actual = percentiles[label]
                if actual is not None:
                    assert isinstance(actual, (int, float))
                    assert not isinstance(actual, bool)
                    assert math.isfinite(actual) and actual >= 0
                if expected is None:
                    assert actual is None
                else:
                    assert actual is not None
                    assert math.isclose(actual, expected)
        attempted = [
            run["independent_route"]
            for run in cohort
            if run["independent_route"]["attempted"]
        ]
        expected_rate = (
            sum(route["succeeded"] for route in attempted) / len(attempted)
            if attempted
            else None
        )
        actual_rate = summary["independent_route_success_rate"]
        expected_route_complete = all(
            run["independent_route"]["capability"] == "available" for run in cohort
        )
        assert (
            summary["independent_route_measurement_complete"]
            is expected_route_complete
        )
        assert summary["independent_route_measurement_complete"], (
            "release comparison fails closed when provider-attempt telemetry "
            "is unavailable"
        )
        if expected_rate is None:
            assert actual_rate is None
        else:
            assert actual_rate is not None
            assert math.isclose(actual_rate, expected_rate)


def _approved_manifest() -> dict[str, Any]:
    manifest = copy.deepcopy(_load_json(YOUTUBE_MANIFEST))
    manifest["approval"] = {
        "status": "approved",
        "approved_by": "release-owner",
        "approved_at": "2026-09-20T12:00:00Z",
    }
    cases = [*manifest["shorts"], *manifest["videos"]]
    for index, case in enumerate(cases):
        video_id = f"case{index:07d}"
        if case["case_id"].startswith("short-"):
            case["url"] = f"https://www.youtube.com/shorts/{video_id}"
        else:
            case["url"] = f"https://www.youtube.com/watch?v={video_id}"
    cases[0]["traits"] = sorted(REQUIRED_TRAITS)
    return manifest


def _valid_evidence(case_kinds: dict[str, str]) -> dict[str, Any]:
    runs = []
    for case_id, kind in case_kinds.items():
        for cache_state in ("cold", "warm"):
            runs.append(
                {
                    "case_id": case_id,
                    "kind": kind,
                    "cache_state": cache_state,
                    "latency_seconds": {
                        "resolve": 1.0,
                        "first_byte": 1.0,
                        "materialize": 1.0,
                        "deliver": 1.0,
                    },
                    "measurement": {
                        "first_byte": {
                            "availability": "measured",
                            "method": "task11-metric",
                        },
                        "downloaded_bytes": {
                            "availability": "measured",
                            "method": "structured-request-bytes",
                        },
                    },
                    "full_delivery": True,
                    "failure_stage": None,
                    "http_failures": [],
                    "bytes_downloaded": 1,
                    "bytes_wasted": 0,
                    "process_cpu_seconds": 0.0,
                    "peak_rss_bytes": 134_217_728,
                    "independent_route": {
                        "capability": "available",
                        "provenance": "exact-metric",
                        "attempted": False,
                        "succeeded": False,
                        "route_class": None,
                    },
                }
            )
    empty_causes = {
        "rate-limited": 0,
        "forbidden": 0,
        "expired-signature": 0,
        "geo-blocked": 0,
        "bot-detection": 0,
        "upstream-policy": 0,
        "unknown": 0,
    }
    summaries = []
    for kind in ("short", "video"):
        for cache_state in ("cold", "warm"):
            summaries.append(
                {
                    "kind": kind,
                    "cache_state": cache_state,
                    "sample_count": 36,
                    "full_delivery_rate": 1.0,
                    "latency_seconds": {
                        phase: {"p50": 1.0, "p95": 1.0}
                        for phase in ("resolve", "first_byte", "materialize", "deliver")
                    },
                    "http_403_causes": empty_causes.copy(),
                    "http_429_causes": empty_causes.copy(),
                    "bytes_downloaded": 36,
                    "measurement_complete": True,
                    "bytes_wasted": 0,
                    "process_cpu_seconds": 0.0,
                    "peak_rss_bytes": {
                        "p50": 134_217_728,
                        "p95": 134_217_728,
                        "max": 134_217_728,
                    },
                    "within_current_memory_limit": True,
                    "independent_route_success_rate": None,
                    "independent_route_measurement_complete": True,
                }
            )
    return {
        "schema_version": 1,
        "release_sha": "a" * 40,
        "manifest_sha256": "b" * 64,
        "collected_at": "2026-09-20T15:00:00Z",
        "source": "production-vps",
        "production_ip_attested": True,
        "current_memory_limit_bytes": 2_147_483_648,
        "runtime_profile": "candidate",
        "image_id": "sha256:" + "c" * 64,
        "image_reference": "ghcr.io/example/bot@sha256:" + "d" * 64,
        "identity_binding": "candidate-release-and-digest",
        "redaction": {
            "urls_hashed": True,
            "tokens_removed": True,
            "signed_queries_removed": True,
            "cookies_removed": True,
        },
        "windows": [
            {
                "window_id": f"window-{index}",
                "started_at": f"2026-09-2{index}T15:00:00Z",
                "runs": copy.deepcopy(runs),
                "reconciliations": [],
            }
            for index in range(1, 4)
        ],
        "summaries": summaries,
    }


def test_youtube_acceptance_manifest_reserves_12_shorts_and_12_videos() -> None:
    manifest = _load_json(YOUTUBE_MANIFEST)
    schema = _load_json(YOUTUBE_SCHEMA)

    assert manifest["schema_version"] == 1
    assert len(manifest["shorts"]) == 12
    assert len(manifest["videos"]) == 12
    assert (
        len({case["case_id"] for case in [*manifest["shorts"], *manifest["videos"]]})
        == 24
    )
    assert schema["properties"]["shorts"]["minItems"] == 12
    assert schema["properties"]["shorts"]["maxItems"] == 12
    assert schema["properties"]["videos"]["minItems"] == 12
    assert schema["properties"]["videos"]["maxItems"] == 12


def test_pending_or_incomplete_manifest_cannot_be_used_for_live_acceptance() -> None:
    manifest = _load_json(YOUTUBE_MANIFEST)
    _require_approved_manifest(manifest)

    pending = copy.deepcopy(manifest)
    pending["approval"] = {
        "status": "pending",
        "approved_by": None,
        "approved_at": None,
    }
    with pytest.raises(AssertionError, match="explicitly approved"):
        _require_approved_manifest(pending)

    incomplete = copy.deepcopy(manifest)
    incomplete["shorts"][0]["url"] = None
    with pytest.raises((AssertionError, ValidationError)):
        _require_approved_manifest(incomplete)


def test_evidence_schema_requires_redacted_three_window_stage_results() -> None:
    schema = _load_json(EVIDENCE_SCHEMA)

    assert {
        "release_sha",
        "manifest_sha256",
        "source",
        "production_ip_attested",
        "runtime_profile",
        "image_id",
        "image_reference",
        "identity_binding",
        "redaction",
        "windows",
        "summaries",
    } <= set(schema["required"])
    assert schema["properties"]["windows"]["minItems"] == 3
    assert schema["properties"]["windows"]["maxItems"] == 3
    run_required = set(schema["$defs"]["run"]["required"])
    assert {
        "case_id",
        "kind",
        "cache_state",
        "latency_seconds",
        "measurement",
        "full_delivery",
        "http_failures",
        "bytes_downloaded",
        "bytes_wasted",
        "process_cpu_seconds",
        "independent_route",
    } <= run_required


def test_task14_commands_target_actual_vps_path_and_require_image_binding() -> None:
    documentation = (ROOT / "docs" / "acceptance.md").read_text(encoding="utf-8")
    controlled_run = documentation.split("## Controlled Task 14 run", 1)[1]
    assert "--project-dir /opt/ytdlbot" in controlled_run
    assert "--project-dir /srv/ytdlbot" not in controlled_run
    assert "--expected-image-id" in controlled_run
    assert "--legacy-attestation" in controlled_run
    assert "5c1aaa1b786a92e979f80b09033b71978cbae701" in controlled_run
    assert (
        "sha256:2b109643e8cb04386b2508d112cbab02b1648e2b02f1827356039a59448ef3cf"
        in controlled_run
    )


def test_unavailable_measurement_is_schema_valid_but_fails_release_criterion() -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    run = evidence["windows"][0]["runs"][0]
    run["latency_seconds"]["first_byte"] = None
    run["measurement"]["first_byte"] = {
        "availability": "unavailable",
        "method": "unavailable",
    }
    summary = next(
        item
        for item in evidence["summaries"]
        if item["kind"] == run["kind"] and item["cache_state"] == run["cache_state"]
    )
    summary["measurement_complete"] = False
    _validate_schema(evidence, EVIDENCE_SCHEMA)

    with pytest.raises(AssertionError, match="fails closed"):
        _validate_evidence(evidence, case_kinds)


def test_unavailable_provider_attempt_telemetry_fails_release_criterion() -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    run = evidence["windows"][0]["runs"][0]
    run["independent_route"] = {
        "capability": "unavailable",
        "provenance": "unavailable",
        "attempted": None,
        "succeeded": None,
        "route_class": None,
    }
    summary = next(
        item
        for item in evidence["summaries"]
        if item["kind"] == run["kind"] and item["cache_state"] == run["cache_state"]
    )
    summary["independent_route_measurement_complete"] = False
    _validate_schema(evidence, EVIDENCE_SCHEMA)

    with pytest.raises(AssertionError, match="provider-attempt telemetry"):
        _validate_evidence(evidence, case_kinds)


def test_ci_pins_draft_2020_schema_validator_with_format_support() -> None:
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    assert "jsonschema[format]==4.26.0" in requirements.splitlines()


def test_live_evidence_accepts_complete_memory_and_latency_contract() -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())

    _validate_evidence(_valid_evidence(case_kinds), case_kinds)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda evidence: evidence["windows"][0]["runs"][0].update(
            {"peak_rss_bytes": -1}
        ),
        lambda evidence: evidence["summaries"][0]["peak_rss_bytes"].update({"p95": 1}),
        lambda evidence: evidence["summaries"][0]["peak_rss_bytes"].update({"max": 1}),
        lambda evidence: evidence["summaries"][0].update(
            {"within_current_memory_limit": False}
        ),
        lambda evidence: evidence.update(
            {"current_memory_limit_bytes": 64 * 1024 * 1024}
        ),
        lambda evidence: (
            evidence.update({"current_memory_limit_bytes": 64 * 1024 * 1024}),
            [
                summary.update({"within_current_memory_limit": False})
                for summary in evidence["summaries"]
            ],
        ),
    ),
)
def test_live_evidence_rejects_inconsistent_memory_summaries_and_limit(
    mutate,
) -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    _validate_evidence(evidence, case_kinds)
    mutate(evidence)

    with pytest.raises((AssertionError, ValidationError)):
        _validate_evidence(evidence, case_kinds)


@pytest.mark.parametrize("invalid_latency", (None, math.inf, math.nan))
def test_full_delivery_rejects_missing_or_nonfinite_stage_latency(
    invalid_latency,
) -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    _validate_evidence(evidence, case_kinds)
    evidence["windows"][0]["runs"][0]["latency_seconds"]["resolve"] = invalid_latency

    with pytest.raises((AssertionError, ValidationError)):
        _validate_evidence(evidence, case_kinds)


def test_all_success_null_stage_latencies_are_rejected() -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    _validate_evidence(evidence, case_kinds)
    for window in evidence["windows"]:
        for run in window["runs"]:
            run["latency_seconds"] = {
                "resolve": None,
                "first_byte": None,
                "materialize": None,
                "deliver": None,
            }
    for summary in evidence["summaries"]:
        summary["latency_seconds"] = {
            "resolve": {"p50": None, "p95": None},
            "first_byte": {"p50": None, "p95": None},
            "materialize": {"p50": None, "p95": None},
            "deliver": {"p50": None, "p95": None},
        }

    with pytest.raises((AssertionError, ValidationError)):
        _validate_evidence(evidence, case_kinds)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda manifest: manifest.update({"token": "secret"}),
        lambda manifest: manifest["approval"].update({"approved_at": "not-a-date"}),
        lambda manifest: manifest["shorts"][0].update({"case_id": "video-01"}),
        lambda manifest: manifest["videos"][0].update(
            {"url": "https://example.com/watch?v=case0000012"}
        ),
    ),
)
def test_approved_manifest_rejects_schema_and_bucket_violations(mutate) -> None:
    manifest = _approved_manifest()
    mutate(manifest)

    with pytest.raises((AssertionError, ValidationError)):
        _require_approved_manifest(manifest)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda evidence: evidence.update({"raw_url": "https://secret.example/x"}),
        lambda evidence: evidence["windows"][0]["runs"].append(
            copy.deepcopy(evidence["windows"][0]["runs"][0])
        ),
        lambda evidence: evidence["windows"][0]["runs"][0]["independent_route"].update(
            {"attempted": False, "succeeded": True, "route_class": "paid"}
        ),
        lambda evidence: evidence["windows"][0]["runs"][0]["http_failures"].append(
            {"status": 403, "cause": "token=https://secret.example"}
        ),
        lambda evidence: evidence["windows"][1].update(
            {"started_at": evidence["windows"][0]["started_at"]}
        ),
        lambda evidence: evidence["windows"][0].update(
            {"window_id": "window-1-token-abc123"}
        ),
        lambda evidence: evidence["summaries"][0].update(
            {"sample_count": 999, "full_delivery_rate": 0.5}
        ),
        lambda evidence: evidence.update(
            {"identity_binding": "legacy-deployment-attestation"}
        ),
    ),
)
def test_live_evidence_rejects_duplicates_secrets_and_inconsistent_summaries(
    mutate,
) -> None:
    manifest = _approved_manifest()
    _require_approved_manifest(manifest)
    case_kinds = {
        case["case_id"]: kind
        for kind, bucket in (("short", "shorts"), ("video", "videos"))
        for case in manifest[bucket]
    }
    evidence = _valid_evidence(case_kinds)
    mutate(evidence)

    with pytest.raises((AssertionError, ValidationError)):
        _validate_evidence(evidence, case_kinds)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda evidence: evidence["summaries"][0]["latency_seconds"]["resolve"].update(
            {"p95": 9.0}
        ),
        lambda evidence: evidence["windows"][0]["runs"][0].update(
            {"full_delivery": True, "failure_stage": "deliver"}
        ),
        lambda evidence: (
            evidence["windows"][0]["runs"][0].update(
                {"full_delivery": False, "failure_stage": None}
            ),
            evidence["summaries"][0].update({"full_delivery_rate": 35 / 36}),
        ),
    ),
)
def test_live_evidence_rejects_inconsistent_latency_and_delivery_semantics(
    mutate,
) -> None:
    case_kinds = _require_approved_manifest(_approved_manifest())
    evidence = _valid_evidence(case_kinds)
    mutate(evidence)

    with pytest.raises((AssertionError, ValidationError)):
        _validate_evidence(evidence, case_kinds)


def test_offline_acceptance_includes_cross_boundary_release_regressions() -> None:
    assert REQUIRED_OFFLINE_ACCEPTANCE_NODES <= set(OFFLINE_ACCEPTANCE_NODES)


def test_offline_acceptance_aggregates_release_critical_contracts() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *OFFLINE_ACCEPTANCE_NODES,
            "--no-cov",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_integration_workflow_keeps_network_diagnostics_opt_in() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "integration.yml").read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )
    live_input = workflow["on"]["workflow_dispatch"]["inputs"]["run_live_diagnostics"]
    assert live_input["default"] == "false"
    steps = workflow["jobs"]["integration"]["steps"]
    offline = next(
        step for step in steps if step.get("name") == "Run offline acceptance"
    )
    live = next(
        step for step in steps if step.get("name") == "Run opt-in network diagnostics"
    )
    assert "tests/acceptance/test_media_release.py" in offline["run"]
    assert "workflow_dispatch" in live["if"]
    assert "run_live_diagnostics" in live["if"]
    assert "tests/test_integration_real.py" in live["run"]


@pytest.mark.integration
def test_live_evidence_matches_approved_manifest_and_redaction_contract() -> None:
    manifest_path = Path(
        os.environ.get("YOUTUBE_ACCEPTANCE_MANIFEST", YOUTUBE_MANIFEST)
    )
    evidence_raw = os.environ.get("MEDIA_RELEASE_EVIDENCE")
    assert evidence_raw, "Task 14 must supply MEDIA_RELEASE_EVIDENCE"
    evidence_path = Path(evidence_raw)
    manifest = _load_json(manifest_path)
    case_ids = _require_approved_manifest(manifest)
    evidence = _load_json(evidence_path)
    assert (
        evidence["manifest_sha256"]
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
    _validate_evidence(evidence, case_ids)
