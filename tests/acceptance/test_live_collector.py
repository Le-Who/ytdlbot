from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "collect-media-release-evidence.py"
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "youtube-acceptance.json"
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "media-release-evidence.schema.json"


def _load_collector() -> Any:
    spec = importlib.util.spec_from_file_location(
        "media_evidence_collector", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def collector() -> Any:
    return _load_collector()


def _observation(*, successful: bool = True, cache_hit: bool = False) -> dict[str, Any]:
    phases = {
        phase: {
            "before_count": 10,
            "before_sum": 20.0,
            "after_count": 11,
            "after_sum": 20.0 + value,
        }
        for phase, value in {
            "resolve": 0.11,
            "first_byte": 0.22,
            "materialize": 0.33,
            "deliver": 0.44,
        }.items()
    }
    if cache_hit:
        for phase in ("resolve", "first_byte", "materialize"):
            phases[phase]["after_count"] = phases[phase]["before_count"]
            phases[phase]["after_sum"] = phases[phase]["before_sum"]
    return {
        "attribution_confirmed": True,
        "bypassed_phases": (
            ["resolve", "first_byte", "materialize"] if cache_hit else []
        ),
        "file_id_hits_before": 4,
        "file_id_hits_after": 5 if cache_hit else 4,
        "phase_metrics": phases,
        "pipeline_results_before": {"success": 7, "failed": 2},
        "pipeline_results_after": {
            "success": 8 if successful else 7,
            "failed": 2 if successful else 3,
        },
        "wasted_bytes_before": 100,
        "wasted_bytes_after": 103,
        "measurement": {
            "first_byte": {
                "availability": "measured",
                "method": "file-id-cache" if cache_hit else "task11-metric",
            },
            "downloaded_bytes": {
                "availability": "measured",
                "method": "file-id-cache" if cache_hit else "structured-request-bytes",
                "value": 0 if cache_hit else 900,
            },
        },
        "cpu_seconds_before": 8.0,
        "cpu_seconds_after": 8.25,
        "rss_bytes_samples": [25_000_000, 27_000_000, 26_000_000],
        "job_state": "completed" if successful else "failed",
        "delivery_outcomes": ["success"] if successful else ["failed"],
        "http_failures": [],
        "independent_route": {
            "capability": "available",
            "provenance": "exact-metric",
            "attempted": False,
            "succeeded": False,
            "route_class": None,
        },
        "correlated_events": [
            "webhook-accepted",
            "job-completed" if successful else "job-failed",
        ],
    }


class FakeAdapter:
    def __init__(self, *, safe_evict: bool = True, timeout_first: bool = False) -> None:
        self.safe_evict = safe_evict
        self.timeout_first = timeout_first
        self.calls: list[tuple[str, str, str | None]] = []
        self.cancelled: list[str] = []

    def identity(self) -> dict[str, Any]:
        return {
            "current_memory_limit_bytes": 536_870_912,
            "production_ip_attested": True,
            "release": "a" * 40,
            "runtime_profile": "candidate",
            "image_id": "sha256:" + "b" * 64,
            "image_reference": "ghcr.io/example/bot@sha256:" + "c" * 64,
            "identity_binding": "candidate-release-and-digest",
        }

    def evict_case(self, case: Any) -> bool:
        self.calls.append(("evict", case.case_id, None))
        return self.safe_evict

    def observe(
        self,
        *,
        update: dict[str, Any],
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        cache_state = "cold" if correlation_id.endswith(":cold") else "warm"
        case_id = correlation_id.rsplit(":", 2)[-2]
        self.calls.append(("observe", case_id, cache_state))
        if self.timeout_first:
            self.timeout_first = False
            raise TimeoutError("bounded observation timeout")
        assert update["message"]["entities"] == [
            {"type": "bot_command", "offset": 0, "length": 4}
        ]
        assert update["message"]["text"].startswith("/mp4 https://")
        assert "chat" not in update["message"]
        assert "from" not in update["message"]
        return _observation(cache_hit=cache_state == "warm")

    def cancel(self, correlation_id: str) -> bool:
        self.cancelled.append(correlation_id)
        return True


def _collect(
    collector: Any,
    tmp_path: Path,
    adapter: FakeAdapter,
    *,
    window_id: str = "window-1",
    output_name: str = "window.json",
    collected_at: str = "2026-09-20T12:00:00Z",
) -> Path:
    output = tmp_path / output_name
    collector.collect_window(
        manifest_path=MANIFEST_PATH,
        output_path=output,
        window_id=window_id,
        release_sha="a" * 40,
        adapter=adapter,
        correlation_prefix="release-proof",
        timeout_seconds=3.0,
        collected_at=collected_at,
    )
    return output


def test_collects_exact_cold_warm_sequence_without_persisting_secrets_or_urls(
    collector: Any,
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter()

    output = _collect(collector, tmp_path, adapter)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 48
    assert [(run["case_id"], run["cache_state"]) for run in payload["runs"][:4]] == [
        ("short-01", "cold"),
        ("short-01", "warm"),
        ("short-02", "cold"),
        ("short-02", "warm"),
    ]
    assert len([call for call in adapter.calls if call[0] == "evict"]) == 24
    assert len([call for call in adapter.calls if call[0] == "observe"]) == 48
    persisted = output.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert all(
        case["url"] not in persisted
        for bucket in ("shorts", "videos")
        for case in manifest[bucket]
    )
    first = payload["runs"][0]
    assert first["latency_seconds"] == {
        "resolve": pytest.approx(0.11),
        "first_byte": pytest.approx(0.22),
        "materialize": pytest.approx(0.33),
        "deliver": pytest.approx(0.44),
    }
    assert first["full_delivery"] is True
    assert first["bytes_downloaded"] == 900
    assert first["measurement"]["first_byte"]["method"] == "task11-metric"
    assert first["bytes_wasted"] == 3
    assert first["process_cpu_seconds"] == pytest.approx(0.25)
    assert first["peak_rss_bytes"] == 27_000_000
    warm = payload["runs"][1]
    assert warm["latency_seconds"] == {
        "resolve": 0.0,
        "first_byte": 0.0,
        "materialize": 0.0,
        "deliver": pytest.approx(0.44),
    }
    assert payload["image_id"] == "sha256:" + "b" * 64
    assert payload["in_flight"] is None


def test_resume_skips_completed_case_cache_pairs(
    collector: Any, tmp_path: Path
) -> None:
    first_adapter = FakeAdapter()
    output = _collect(collector, tmp_path, first_adapter)
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["runs"].pop()
    output.write_text(json.dumps(payload), encoding="utf-8")
    resumed_adapter = FakeAdapter()

    _collect(collector, tmp_path, resumed_adapter)

    assert [call for call in resumed_adapter.calls if call[0] == "observe"] == [
        ("observe", "video-12", "warm")
    ]
    assert not [call for call in resumed_adapter.calls if call[0] == "evict"]
    assert len(json.loads(output.read_text(encoding="utf-8"))["runs"]) == 48
    assert not list(tmp_path.glob(".*.tmp"))


def test_refuses_cold_run_when_exact_case_eviction_is_unavailable(
    collector: Any,
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(safe_evict=False)

    with pytest.raises(collector.UnsafeColdCacheError, match="short-01"):
        _collect(collector, tmp_path, adapter)

    assert not [call for call in adapter.calls if call[0] == "observe"]


def test_timeout_leaves_atomic_in_flight_reservation_and_resume_hard_stops(
    collector: Any,
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(timeout_first=True)
    output = tmp_path / "timed-out.json"

    with pytest.raises(collector.ObservationError, match="cancelled after timeout"):
        _collect(collector, tmp_path, adapter, output_name=output.name)

    assert adapter.cancelled == ["release-proof:window-1:short-01:cold"]
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runs"] == []
    assert payload["in_flight"] == {
        "case_id": "short-01",
        "kind": "short",
        "cache_state": "cold",
        "correlation_id": "release-proof:window-1:short-01:cold",
        "update_id": collector._update_id("release-proof:window-1:short-01:cold"),
        "reserved_at": "2026-09-20T12:00:00Z",
        "state": "IN_FLIGHT",
    }

    resumed = FakeAdapter()
    with pytest.raises(collector.EvidenceValidationError, match="IN_FLIGHT"):
        _collect(collector, tmp_path, resumed, output_name=output.name)
    assert not [call for call in resumed.calls if call[0] == "observe"]


def test_unconfirmed_timeout_stops_window_as_unattributable(
    collector: Any,
    tmp_path: Path,
) -> None:
    class UncancellableAdapter(FakeAdapter):
        def cancel(self, correlation_id: str) -> bool:
            self.cancelled.append(correlation_id)
            return False

    adapter = UncancellableAdapter(timeout_first=True)
    output = tmp_path / "unconfirmed-timeout.json"

    with pytest.raises(collector.ObservationError, match="could not be confirmed"):
        _collect(collector, tmp_path, adapter, output_name=output.name)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runs"] == []
    assert payload["in_flight"]["state"] == "IN_FLIGHT"


def test_observation_rejects_ambiguous_process_wide_metric_delta(
    collector: Any,
    tmp_path: Path,
) -> None:
    class AmbiguousAdapter(FakeAdapter):
        def observe(self, **kwargs: Any) -> dict[str, Any]:
            observation = super().observe(**kwargs)
            observation["phase_metrics"]["resolve"]["after_count"] = 12
            return observation

    output = tmp_path / "ambiguous.json"
    with pytest.raises(collector.ObservationError, match="concurrent activity"):
        _collect(
            collector,
            tmp_path,
            AmbiguousAdapter(),
            output_name=output.name,
        )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["runs"] == []
    assert payload["in_flight"]["state"] == "IN_FLIGHT"

    resumed = FakeAdapter()
    with pytest.raises(
        collector.EvidenceValidationError, match="audited reconciliation"
    ):
        _collect(collector, tmp_path, resumed, output_name=output.name)
    assert not [call for call in resumed.calls if call[0] == "observe"]


def test_reservation_is_persisted_before_submit_and_requires_audited_reconciliation(
    collector: Any, tmp_path: Path
) -> None:
    output = tmp_path / "crashed.json"

    class CrashAfterReservation(FakeAdapter):
        def observe(self, **kwargs: Any) -> dict[str, Any]:
            reservation = json.loads(output.read_text(encoding="utf-8"))["in_flight"]
            assert reservation["state"] == "IN_FLIGHT"
            assert reservation["correlation_id"] == kwargs["correlation_id"]
            assert reservation["update_id"] == kwargs["update"]["update_id"]
            raise collector.ObservationError("submit outcome unknown")

    with pytest.raises(collector.ObservationError, match="unknown"):
        _collect(collector, tmp_path, CrashAfterReservation(), output_name=output.name)

    with pytest.raises(collector.EvidenceValidationError, match="IN_FLIGHT"):
        _collect(collector, tmp_path, FakeAdapter(), output_name=output.name)

    collector.reconcile_in_flight(
        output_path=output,
        decision="confirmed-not-accepted",
        audited_by="release-owner",
        reconciled_at="2026-09-20T12:15:00Z",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["in_flight"] is None
    assert payload["reconciliations"][0]["decision"] == "confirmed-not-accepted"
    _collect(collector, tmp_path, FakeAdapter(), output_name=output.name)


def test_unavailable_measurements_are_null_and_make_summary_incomplete(
    collector: Any, tmp_path: Path
) -> None:
    observation = _observation()
    observation["measurement"] = {
        "first_byte": {"availability": "unavailable", "method": "unavailable"},
        "downloaded_bytes": {
            "availability": "unavailable",
            "method": "unavailable",
            "value": None,
        },
    }
    observation["unavailable_phases"] = ["first_byte"]
    run = collector._run_from_observation(
        case=collector.CaseSpec("video-01", "video", "https://invalid.example"),
        cache_state="cold",
        observation=observation,
    )

    assert run["latency_seconds"]["first_byte"] is None
    assert run["bytes_downloaded"] is None
    summary = collector._summary(
        [run], kind="video", cache_state="cold", memory_limit=536_870_912
    )
    assert summary["measurement_complete"] is False
    assert summary["bytes_downloaded"] is None


def test_unavailable_provider_attempt_capability_is_preserved_and_fails_summary(
    collector: Any,
) -> None:
    observation = _observation()
    observation["independent_route"] = {
        "capability": "unavailable",
        "provenance": "unavailable",
        "attempted": None,
        "succeeded": None,
        "route_class": None,
    }
    run = collector._run_from_observation(
        case=collector.CaseSpec("video-01", "video", "https://invalid.example"),
        cache_state="cold",
        observation=observation,
    )

    assert run["independent_route"]["attempted"] is None
    summary = collector._summary(
        [run], kind="video", cache_state="cold", memory_limit=536_870_912
    )
    assert summary["independent_route_measurement_complete"] is False


def test_refuses_unattested_host_and_unsafe_correlated_log_values(
    collector: Any,
    tmp_path: Path,
) -> None:
    class UnattestedAdapter(FakeAdapter):
        def identity(self) -> dict[str, Any]:
            result = super().identity()
            result["production_ip_attested"] = False
            return result

    with pytest.raises(collector.EvidenceValidationError, match="production-IP"):
        _collect(
            collector, tmp_path, UnattestedAdapter(), output_name="unattested.json"
        )

    class UnsafeLogAdapter(FakeAdapter):
        def observe(self, **kwargs: Any) -> dict[str, Any]:
            result = super().observe(**kwargs)
            result["correlated_events"].append("https://signed.invalid/?token=secret")
            return result

    with pytest.raises(collector.ObservationError, match="unsafe events"):
        _collect(collector, tmp_path, UnsafeLogAdapter(), output_name="unsafe-log.json")

    class InvalidImageBindingAdapter(FakeAdapter):
        def identity(self) -> dict[str, Any]:
            result = super().identity()
            result["image_reference"] = "mutable-image:latest"
            return result

    with pytest.raises(collector.EvidenceValidationError, match="image binding"):
        _collect(
            collector,
            tmp_path,
            InvalidImageBindingAdapter(),
            output_name="invalid-image.json",
        )


def test_finalize_requires_three_fixed_windows_and_computes_four_summaries(
    collector: Any,
    tmp_path: Path,
) -> None:
    windows = [
        _collect(
            collector,
            tmp_path,
            FakeAdapter(),
            window_id=f"window-{index}",
            output_name=f"window-{index}.json",
            collected_at=f"2026-09-2{index}T12:00:00Z",
        )
        for index in range(1, 4)
    ]
    output = tmp_path / "evidence.json"

    collector.finalize_evidence(
        manifest_path=MANIFEST_PATH,
        schema_path=SCHEMA_PATH,
        window_paths=windows,
        output_path=output,
        collected_at="2026-09-20T13:00:00Z",
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [window["window_id"] for window in evidence["windows"]] == [
        "window-1",
        "window-2",
        "window-3",
    ]
    assert [(item["kind"], item["cache_state"]) for item in evidence["summaries"]] == [
        ("short", "cold"),
        ("short", "warm"),
        ("video", "cold"),
        ("video", "warm"),
    ]
    assert all(item["sample_count"] == 36 for item in evidence["summaries"])
    assert all(item["full_delivery_rate"] == 1.0 for item in evidence["summaries"])
    assert evidence["current_memory_limit_bytes"] == 536_870_912
    assert evidence["image_id"] == "sha256:" + "b" * 64
    assert evidence["image_reference"].endswith("@sha256:" + "c" * 64)
    assert evidence["identity_binding"] == "candidate-release-and-digest"
    assert evidence["redaction"] == {
        "urls_hashed": True,
        "tokens_removed": True,
        "signed_queries_removed": True,
        "cookies_removed": True,
    }


def test_finalize_rejects_missing_or_duplicate_window_ids(
    collector: Any, tmp_path: Path
) -> None:
    first = _collect(collector, tmp_path, FakeAdapter(), output_name="one.json")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(first.read_bytes())

    with pytest.raises(collector.EvidenceValidationError, match="window-1..3"):
        collector.finalize_evidence(
            manifest_path=MANIFEST_PATH,
            schema_path=SCHEMA_PATH,
            window_paths=[first, duplicate],
            output_path=tmp_path / "invalid.json",
            collected_at="2026-09-20T13:00:00Z",
        )


def test_command_adapter_keeps_sensitive_values_off_argv_and_errors(
    collector: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[list[str], dict[str, Any]]] = []

    def successful_run(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        invocations.append((command, json.loads(kwargs["input"])))
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_observation()), stderr=""
        )

    monkeypatch.setattr(collector.subprocess, "run", successful_run)
    adapter = collector.JsonCommandAdapter(["trusted-adapter", "--container", "bot"])
    update = {"message": {"text": "/mp4 https://example.invalid/raw"}}

    adapter.observe(
        update=update,
        correlation_id="safe-correlation",
        timeout_seconds=3.0,
    )

    command, stdin = invocations[0]
    assert command == ["trusted-adapter", "--container", "bot", "observe"]
    assert "example.invalid" not in " ".join(command)
    assert "webhook_secret" not in stdin

    def failed_run(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            command,
            9,
            stdout="https://signed.invalid/?token=do-not-echo",
            stderr="TELEGRAM_SECRET_TOKEN=do-not-echo",
        )

    monkeypatch.setattr(collector.subprocess, "run", failed_run)
    with pytest.raises(collector.ObservationError) as raised:
        adapter.identity()
    assert "do-not-echo" not in str(raised.value)
