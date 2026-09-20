from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    "tests/test_health_readiness.py::test_ready_reports_release_limit_store_and_required_local_api",
    "tests/core/test_job_store.py::test_checkpointed_job_is_recovered_after_worker_restart",
    "tests/deploy/test_release_scripts.py::test_interrupted_activation_runs_rollback",
    "tests/test_pipeline_entrypoints.py::test_callback_codec_emits_v2_and_accepts_previous_shape",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_approved_manifest(manifest: dict[str, Any]) -> set[str]:
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
    traits = {trait for case in cases for trait in case["traits"]}
    assert REQUIRED_TRAITS <= traits
    return {case["case_id"] for case in cases}


def _validate_evidence(evidence: dict[str, Any], case_ids: set[str]) -> None:
    assert re.fullmatch(r"[0-9a-f]{40}", evidence["release_sha"])
    assert re.fullmatch(r"[0-9a-f]{64}", evidence["manifest_sha256"])
    assert evidence["source"] == "production-vps"
    assert evidence["production_ip_attested"] is True
    assert evidence["redaction"] == {
        "urls_hashed": True,
        "tokens_removed": True,
        "signed_queries_removed": True,
        "cookies_removed": True,
    }
    windows = evidence["windows"]
    assert len(windows) == 3
    assert len({window["window_id"] for window in windows}) == 3
    expected_runs = {
        (case_id, cache) for case_id in case_ids for cache in ("cold", "warm")
    }
    for window in windows:
        actual_runs = {(run["case_id"], run["cache_state"]) for run in window["runs"]}
        assert actual_runs == expected_runs
        for run in window["runs"]:
            assert set(run["latency_seconds"]) == {
                "resolve",
                "first_byte",
                "materialize",
                "deliver",
            }
            assert isinstance(run["full_delivery"], bool)
            assert run["bytes_downloaded"] >= 0
            assert run["bytes_wasted"] >= 0
            assert run["process_cpu_seconds"] >= 0
            assert all(
                failure["status"] in (403, 429) and failure["cause"]
                for failure in run["http_failures"]
            )
            assert set(run["independent_route"]) == {
                "attempted",
                "succeeded",
                "route_class",
            }
    summaries = evidence["summaries"]
    assert {(item["kind"], item["cache_state"]) for item in summaries} == {
        (kind, cache) for kind in ("short", "video") for cache in ("cold", "warm")
    }
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
    with pytest.raises(AssertionError, match="explicitly approved"):
        _require_approved_manifest(manifest)

    incomplete = copy.deepcopy(manifest)
    incomplete["approval"] = {
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-09-20T00:00:00Z",
    }
    with pytest.raises(AssertionError):
        _require_approved_manifest(incomplete)


def test_evidence_schema_requires_redacted_three_window_stage_results() -> None:
    schema = _load_json(EVIDENCE_SCHEMA)

    assert {
        "release_sha",
        "manifest_sha256",
        "source",
        "production_ip_attested",
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
        "full_delivery",
        "http_failures",
        "bytes_downloaded",
        "bytes_wasted",
        "process_cpu_seconds",
        "independent_route",
    } <= run_required


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
