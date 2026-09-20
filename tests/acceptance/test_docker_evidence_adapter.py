from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "media-acceptance-docker-adapter.py"


def _load_adapter() -> Any:
    spec = importlib.util.spec_from_file_location("media_docker_adapter", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def adapter_module() -> Any:
    return _load_adapter()


def _metrics(*, complete: bool, legacy: bool = False) -> dict[str, Any]:
    count = 1 if complete else 0
    phases = {
        phase: {"count": count, "sum": value if complete else 0.0}
        for phase, value in {
            "resolve": 0.1,
            "first_byte": 0.2,
            "materialize": 0.3,
            "deliver": 0.4,
        }.items()
    }
    legacy_values = {
        "ytdlbot_extraction_duration_seconds": (1, 0.1),
        "ytdlbot_download_duration_seconds": (1, 0.25),
        "ytdlbot_conversion_duration_seconds": (0, 0.0),
        "ytdlbot_upload_duration_seconds": (1, 0.4),
    }
    return {
        "phases": phases,
        "results": {} if legacy else ({"success": 1} if complete else {}),
        "wasted_bytes": 7 if complete else 5,
        "file_id_hits": 0,
        "providers": {"ytdlp": 1} if complete else {},
        "legacy": {
            name: {
                "count": value[0] if complete else 0,
                "sum": value[1] if complete else 0.0,
            }
            for name, value in legacy_values.items()
        },
        "required_metrics_present": not legacy,
        "legacy_metrics_present": legacy,
    }


class FakeRuntime:
    def __init__(self, module: Any, *, profile: str) -> None:
        self.module = module
        self.profile = profile
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.snapshot_count = 0

    def identity(self) -> Any:
        return self.module.DockerIdentity(
            container_id="a" * 64,
            image_id="sha256:" + "b" * 64,
            configured_image="ghcr.io/example/bot@sha256:" + "c" * 64,
            memory_limit_bytes=self.module.EXPECTED_MEMORY_BYTES,
        )

    def container_call(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        self.calls.append((action, payload))
        if action == "preflight":
            candidate = self.profile == "candidate"
            return {
                "candidate_ready": candidate,
                "legacy_healthy": True,
                "release": "d" * 40 if candidate else None,
                "max_media_file_mb": 2000 if candidate else 900,
                "local_bot_api_ready": candidate,
                "memory_limit_bytes": self.module.EXPECTED_MEMORY_BYTES,
                "rss_bytes": 10_000,
                "secrets_available": True,
                "webhook_template_valid": True,
                "required_metrics_present": candidate,
                "legacy_metrics_present": not candidate,
                "job_store_supported": candidate,
                "cache_modes": (
                    ["legacy-inf", "media-cache"] if candidate else ["legacy-inf"]
                ),
                "bounded_cancel_supported": False,
            }
        if action == "evict-case":
            return {
                "safe": True,
                "mode": "media-cache" if self.profile == "candidate" else "legacy-inf",
                "deleted_count": 1,
            }
        if action == "submit":
            assert "chat" not in payload["update"]["message"]
            assert "from" not in payload["update"]["message"]
            assert "webhook_secret" not in payload
            return {"accepted": True}
        if action == "cancel":
            return {"cancelled": False}
        assert action == "snapshot"
        self.snapshot_count += 1
        complete = self.snapshot_count > 1
        if self.profile == "candidate":
            job = {
                "supported": True,
                "state": "completed" if complete else None,
                "deliveries": (
                    [
                        {
                            "outcome": "success",
                            "finalized": True,
                            "response_confirmed": True,
                        }
                    ]
                    if complete
                    else []
                ),
                "other_jobs": 0,
            }
        else:
            job = {
                "supported": False,
                "state": None,
                "deliveries": [],
                "other_jobs": 0,
            }
        return {
            "metrics": _metrics(
                complete=complete, legacy=self.profile == "legacy-baseline"
            ),
            "cpu_seconds": 3.25 if complete else 3.0,
            "rss_bytes": 12_000 if complete else 10_000,
            "media_sizes": {"path-hash": 900} if complete else {},
            "job": job,
        }

    def correlated_logs(self, *, since: str, correlation_id: str) -> str:
        del since
        if self.profile == "legacy-baseline":
            return "\n".join(
                (
                    json.dumps(
                        {
                            "correlation_id": correlation_id,
                            "timestamp": 100.2,
                            "event": "first_byte",
                        }
                    ),
                    f"{correlation_id} sendVideo status=200 telegram-success",
                )
            )
        return f"{correlation_id} sendVideo status=200"


def _update() -> dict[str, Any]:
    return {
        "update_id": 1_234_567_890,
        "message": {
            "message_id": 1_234_567_891,
            "date": 100,
            "text": "/mp4 https://www.youtube.com/watch?v=approved001",
            "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
        },
    }


def test_candidate_preflight_proves_container_readiness_without_sending(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        runtime_profile="candidate",
    )

    result = adapter.preflight(_update())

    assert result["current_memory_limit_bytes"] == 2 * 1024 * 1024 * 1024
    assert result["runtime_profile"] == "candidate"
    assert result["container_id"] == "a" * 12
    assert [call[0] for call in runtime.calls] == ["preflight"]
    assert "submit" not in json.dumps(runtime.calls)
    assert "BOT_TOKEN" not in json.dumps(result)


def test_candidate_observation_confirms_terminal_delivery_and_resource_deltas(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        runtime_profile="candidate",
    )

    result = adapter.observe(
        update=_update(),
        correlation_id="proof:window-1:video-01:cold",
        timeout_seconds=10,
    )

    assert result["attribution_confirmed"] is True
    assert result["job_state"] == "completed"
    assert result["delivery_outcomes"] == ["success"]
    assert result["pipeline_results_after"] == {"success": 1}
    assert result["artifact_size_samples"] == [0, 900]
    assert result["cpu_seconds_after"] - result["cpu_seconds_before"] == 0.25
    assert result["rss_bytes_samples"] == [10_000, 12_000]
    submit = next(payload for action, payload in runtime.calls if action == "submit")
    assert "BOT_TOKEN" not in json.dumps(submit)
    assert "ADMIN_CHAT_ID" not in json.dumps(submit)
    assert "TELEGRAM_SECRET_TOKEN" not in json.dumps(submit)


def test_legacy_profile_uses_exact_inf_eviction_and_correlated_terminal_logs(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="legacy-baseline")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="e" * 40,
        runtime_profile="legacy-baseline",
        clock=lambda: 100.0,
    )
    case = {
        "case_id": "video-01",
        "kind": "video",
        "url": "https://www.youtube.com/watch?v=approved001",
    }

    preflight = adapter.preflight()
    assert preflight["health_contract"] == "/health"
    assert preflight["max_media_file_mb"] == 900
    assert preflight["job_store_supported"] is False
    assert adapter.evict_case(case) is True
    result = adapter.observe(
        update=_update(),
        correlation_id="proof:window-1:video-01:cold",
        timeout_seconds=10,
    )

    eviction = next(
        payload for action, payload in runtime.calls if action == "evict-case"
    )
    assert eviction == case
    assert result["attribution_confirmed"] is True
    assert result["job_state"] == "completed"
    assert result["delivery_outcomes"] == ["success"]
    assert result["phase_metrics"]["first_byte"]["after_sum"] == pytest.approx(0.2)
    assert result["phase_metrics"]["materialize"]["after_sum"] == pytest.approx(0.25)


def test_embedded_eviction_has_no_global_or_pattern_delete(adapter_module: Any) -> None:
    helper = adapter_module._CONTAINER_HELPER.lower()
    compile(adapter_module._CONTAINER_HELPER, "<container-helper>", "exec")
    assert "flushdb" not in helper
    assert "scan_iter" not in helper
    assert "keys(" not in helper
    assert 'keys = ["inf:" + url]' in helper
    assert 'cache.record_key("metadata", request)' in helper
    assert 'cache.record_key("file-id", request' in helper


def test_candidate_profile_does_not_accept_legacy_readiness(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="legacy-baseline")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        runtime_profile="candidate",
    )

    with pytest.raises(adapter_module.AdapterError, match="candidate readiness"):
        adapter.preflight()


def test_fake_docker_commands_are_project_scoped_and_keep_url_off_argv(
    adapter_module: Any,
    tmp_path: Path,
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services: {bot: {image: example.invalid/bot}}\n", encoding="utf-8"
    )
    commands: list[tuple[list[str], str | None]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append((command, kwargs.get("input")))
        if command[:2] == ["docker", "inspect"]:
            stdout = json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "verified-project",
                                "com.docker.compose.service": "bot",
                            },
                            "Image": "example.invalid/bot@sha256:" + "c" * 64,
                        },
                        "HostConfig": {"Memory": adapter_module.EXPECTED_MEMORY_BYTES},
                        "Image": "sha256:" + "b" * 64,
                    }
                ]
            )
        elif "ps" in command:
            stdout = "a" * 64
        else:
            stdout = '{"safe":true}'
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    runtime = adapter_module.DockerRuntime(
        project_dir=tmp_path,
        project_name="verified-project",
        compose_file=compose,
        run_command=fake_run,
    )

    identity = runtime.identity()
    raw_url = "https://www.youtube.com/watch?v=not-on-command-line"
    result = runtime.container_call("evict-case", {"url": raw_url}, timeout=3.0)

    assert identity.container_id == "a" * 64
    assert result == {"safe": True}
    flattened_commands = "\n".join(" ".join(command) for command, _ in commands)
    assert "verified-project" in flattened_commands
    assert raw_url not in flattened_commands
    assert raw_url in (commands[-1][1] or "")
