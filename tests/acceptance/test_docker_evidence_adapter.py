from __future__ import annotations

import importlib.util
import json
import os
import shutil
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
        "provider_attempts": {"ytdlp": 1} if complete else {},
        "provider_attempts_present": not legacy,
        "active_downloads": 0,
        "queue_depth": 0,
        "legacy": {
            name: {
                "count": value[0] if complete else 0,
                "sum": value[1] if complete else 0.0,
            }
            for name, value in legacy_values.items()
        },
        "legacy_results": {
            "total": 1 if complete and legacy else 0,
            "success": 1 if complete and legacy else 0,
            "failed": 0,
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
        self.log_count = 0
        self.submitted = False
        self.telegram_connections = 0
        self.legacy_calls: list[tuple[str, dict[str, Any], str]] = []

    def identity(self) -> Any:
        return self.module.DockerIdentity(
            container_id="a" * 64,
            image_id="sha256:" + "b" * 64,
            configured_image="ghcr.io/example/bot@sha256:" + "c" * 64,
            memory_limit_bytes=self.module.EXPECTED_MEMORY_BYTES,
            host_pid=1234,
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
                "active_downloads_present": True,
                "queue_depth_present": True,
                "job_store_supported": candidate,
                "cache_modes": (
                    ["legacy-inf", "media-cache"] if candidate else ["legacy-inf"]
                ),
                "bounded_cancel_supported": False,
                "process_fence_supported": True,
                "provider_attempts_present": candidate,
                "provider_attempt_capability": (
                    "exact-metric" if candidate else "unavailable"
                ),
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
            self.submitted = True
            return {"accepted": True}
        if action == "cancel":
            return {"cancelled": False}
        assert action == "snapshot"
        self.snapshot_count += 1
        complete = self.submitted
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
                "active_jobs": 0,
            }
        else:
            job = {
                "supported": False,
                "state": None,
                "deliveries": [],
                "other_jobs": 0,
                "active_jobs": 0,
            }
        return {
            "metrics": _metrics(
                complete=complete, legacy=self.profile == "legacy-baseline"
            ),
            "cpu_seconds": 3.25 if complete else 3.0,
            "rss_bytes": 12_000 if complete else 10_000,
            "media_child_processes": 0,
            "process_fence_supported": True,
            "job": job,
        }

    def telegram_connection_count(self) -> int:
        return self.telegram_connections

    def legacy_container_call(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        image_id: str,
        source_container_id: str,
        container_name: str,
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        self.legacy_calls.append((action, payload, container_name))
        assert image_id == "sha256:" + "b" * 64
        assert source_container_id == "a" * 64
        if action == "preflight":
            return {
                "safe": True,
                "orchestrator_imported": True,
                "redis_isolated": True,
                "webhook_not_started": True,
                "admin_chat_configured": True,
                "local_bot_api_configured": True,
                "max_media_file_mb": 900,
                "memory_limit_bytes": self.module.EXPECTED_MEMORY_BYTES,
            }
        assert action == "run-once"
        observations: dict[str, dict[str, Any]] = {}
        for cache_state in ("cold", "warm"):
            observations[cache_state] = {
                "attribution_confirmed": True,
                "bypassed_phases": [],
                "unavailable_phases": ["first_byte"],
                "file_id_hits_before": 0,
                "file_id_hits_after": 0,
                "phase_metrics": {
                    "resolve": {
                        "before_count": 0,
                        "before_sum": 0.0,
                        "after_count": 1,
                        "after_sum": 0.1,
                    },
                    "first_byte": {
                        "before_count": 0,
                        "before_sum": 0.0,
                        "after_count": 0,
                        "after_sum": 0.0,
                    },
                    "materialize": {
                        "before_count": 0,
                        "before_sum": 0.0,
                        "after_count": 1,
                        "after_sum": 0.3,
                    },
                    "deliver": {
                        "before_count": 0,
                        "before_sum": 0.0,
                        "after_count": 1,
                        "after_sum": 0.4,
                    },
                },
                "pipeline_results_before": {"success": 0},
                "pipeline_results_after": {"success": 1, "failed": 0},
                "wasted_bytes_before": 0,
                "wasted_bytes_after": 0,
                "measurement": {
                    "first_byte": {
                        "availability": "unavailable",
                        "method": "unavailable",
                    },
                    "downloaded_bytes": {
                        "availability": "measured",
                        "method": "legacy-correlated-delivered-size",
                        "value": 900,
                    },
                },
                "cpu_seconds_before": 0.0,
                "cpu_seconds_after": 0.25,
                "rss_bytes_samples": [10_000, 12_000],
                "job_state": "completed",
                "delivery_outcomes": ["success"],
                "http_failures": [],
                "failure_stage": None,
                "independent_route": {
                    "capability": "unavailable",
                    "provenance": "unavailable",
                    "attempted": None,
                    "succeeded": None,
                    "route_class": None,
                },
                "correlated_events": ["job-completed", "delivery-success"],
            }
        return {"observation": observations["cold"]}

    def stop_legacy_container(self, container_name: str) -> bool:
        self.legacy_calls.append(("stop", {}, container_name))
        return True

    def logs_since(self, *, since: str) -> str:
        del since
        self.log_count += 1
        if self.profile == "legacy-baseline":
            return 'INFO: 127.0.0.1 - "POST /webhook HTTP/1.1" 200 OK'
        return ""

    def correlated_logs(self, *, since: str, correlation_id: str) -> str:
        del since
        self.log_count += 1
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
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
    )

    result = adapter.preflight(_update())

    assert result["current_memory_limit_bytes"] == 2 * 1024 * 1024 * 1024
    assert result["runtime_profile"] == "candidate"
    assert result["container_id"] == "a" * 12
    assert result["identity_binding"] == "candidate-release-and-digest"
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
        expected_image_id="sha256:" + "b" * 64,
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
    assert result["measurement"]["first_byte"]["method"] == "task11-metric"
    assert result["measurement"]["downloaded_bytes"]["availability"] == "unavailable"
    assert result["wasted_bytes_after"] - result["wasted_bytes_before"] == 2
    assert result["cpu_seconds_after"] - result["cpu_seconds_before"] == 0.25
    assert result["rss_bytes_samples"] == [10_000, 12_000]
    submit = next(payload for action, payload in runtime.calls if action == "submit")
    assert "BOT_TOKEN" not in json.dumps(submit)
    assert "ADMIN_CHAT_ID" not in json.dumps(submit)
    assert "TELEGRAM_SECRET_TOKEN" not in json.dumps(submit)


def test_multiple_task11_first_byte_samples_fail_closed_without_directory_guess(
    adapter_module: Any,
) -> None:
    class MultiSourceRuntime(FakeRuntime):
        def container_call(
            self, action: str, payload: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            result = super().container_call(action, payload, timeout=timeout)
            if action == "snapshot" and self.submitted:
                result["metrics"]["phases"]["first_byte"] = {
                    "count": 2,
                    "sum": 0.3,
                }
            return result

    runtime = MultiSourceRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
    )

    result = adapter.observe(
        update=_update(), correlation_id="proof:multi-source", timeout_seconds=10
    )

    assert result["unavailable_phases"] == ["first_byte"]
    assert result["measurement"]["first_byte"] == {
        "availability": "unavailable",
        "method": "unavailable",
    }
    assert "artifact_size_samples" not in result


def test_legacy_profile_uses_no_send_preflight_and_one_shot_smoke(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="legacy-baseline")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="e" * 40,
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="legacy-baseline",
        legacy_attestation={
            "deployment_sha": "e" * 40,
            "image_id": "sha256:" + "b" * 64,
        },
    )

    preflight = adapter.preflight()
    observation = adapter.observe_isolated(
        case={
            "case_id": "video-01",
            "kind": "video",
            "url": "https://www.youtube.com/watch?v=approved001",
        },
        correlation_id="proof:video-01:cold",
        timeout_seconds=10,
    )

    assert preflight["collection_mode"] == "isolated-one-shot"
    assert observation["job_state"] == "completed"
    assert [call[0] for call in runtime.legacy_calls] == ["preflight", "run-once"]
    assert not any(action == "submit" for action, _ in runtime.calls)


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
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
    )

    with pytest.raises(adapter_module.AdapterError, match="candidate readiness"):
        adapter.preflight()


def test_legacy_requires_exact_image_and_immutable_attestation(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="legacy-baseline")
    with pytest.raises(adapter_module.AdapterError, match="attestation"):
        adapter_module.ProductionDockerAdapter(
            runtime,
            expected_release="e" * 40,
            expected_image_id="sha256:" + "b" * 64,
            runtime_profile="legacy-baseline",
        )
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="e" * 40,
        expected_image_id="sha256:" + "f" * 64,
        runtime_profile="legacy-baseline",
        legacy_attestation={
            "deployment_sha": "e" * 40,
            "image_id": "sha256:" + "f" * 64,
        },
    )
    with pytest.raises(adapter_module.AdapterError, match="image ID"):
        adapter.preflight()


def test_configured_provider_is_not_attempted_without_exact_attempt_delta(
    adapter_module: Any,
) -> None:
    runtime = FakeRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
        external_free_providers=frozenset({"external"}),
    )
    assert adapter._route_outcome({}, {}, {}, {}, "", cache_hit=False) == {
        "capability": "unavailable",
        "provenance": "unavailable",
        "attempted": None,
        "succeeded": None,
        "route_class": None,
    }
    assert adapter._route_outcome(
        {}, {"external": 1}, {}, {"external": 1}, "", cache_hit=False,
        attempt_metric_available=True,
    ) == {
        "capability": "available",
        "provenance": "exact-metric",
        "attempted": True,
        "succeeded": True,
        "route_class": "external-free",
    }


def test_observe_refuses_nonquiescent_candidate_before_submit(
    adapter_module: Any,
) -> None:
    class BusyRuntime(FakeRuntime):
        def container_call(
            self, action: str, payload: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            result = super().container_call(action, payload, timeout=timeout)
            if action == "snapshot":
                result["job"]["active_jobs"] = 1
            return result

    runtime = BusyRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
        sleep=lambda _: None,
    )
    with pytest.raises(adapter_module.AdapterError, match="quiescent"):
        adapter.observe(
            update=_update(), correlation_id="proof:busy", timeout_seconds=10
        )
    assert not any(action == "submit" for action, _ in runtime.calls)


def test_candidate_missing_attempt_telemetry_is_explicitly_unavailable(
    adapter_module: Any,
) -> None:
    class MissingAttemptRuntime(FakeRuntime):
        def container_call(
            self, action: str, payload: dict[str, Any], *, timeout: float
        ) -> dict[str, Any]:
            result = super().container_call(action, payload, timeout=timeout)
            if action == "preflight":
                result["provider_attempts_present"] = False
            elif action == "snapshot":
                result["metrics"]["provider_attempts_present"] = False
                result["metrics"]["provider_attempts"] = {}
            return result

    runtime = MissingAttemptRuntime(adapter_module, profile="candidate")
    adapter = adapter_module.ProductionDockerAdapter(
        runtime,
        expected_release="d" * 40,
        expected_image_id="sha256:" + "b" * 64,
        runtime_profile="candidate",
        external_free_providers=frozenset({"external"}),
    )

    assert adapter.preflight()["provider_attempt_capability"] == "unavailable"
    result = adapter.observe(
        update=_update(), correlation_id="proof:no-attempts", timeout_seconds=10
    )
    assert result["independent_route"] == {
        "capability": "unavailable",
        "provenance": "unavailable",
        "attempted": None,
        "succeeded": None,
        "route_class": None,
    }


def test_container_helper_uses_process_rss_not_cgroup_memory_current(
    adapter_module: Any,
) -> None:
    helper = adapter_module._CONTAINER_HELPER
    assert 'Path("/sys/fs/cgroup/cgroup.procs")' in helper
    assert '"VmRSS:"' in helper
    assert 'read_counter("/sys/fs/cgroup/memory.current")' not in helper
    assert "media_sizes" not in helper


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
                        "State": {"Running": True, "Pid": 1234},
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
        elif command and command[0] == "nsenter":
            stdout = "0 0 127.0.0.1:40000 127.0.0.1:8081\n"
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
    assert runtime.telegram_connection_count() == 1
    assert result == {"safe": True}
    flattened_commands = "\n".join(" ".join(command) for command, _ in commands)
    assert "verified-project" in flattened_commands
    assert raw_url not in flattened_commands
    assert any(raw_url in (input_text or "") for _, input_text in commands)


def test_legacy_one_shot_uses_attested_image_isolated_env_and_stdin_payload(
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
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "safe": True,
                    "orchestrator_imported": True,
                    "redis_isolated": True,
                    "webhook_not_started": True,
                    "admin_chat_configured": True,
                    "local_bot_api_configured": True,
                    "max_media_file_mb": 900,
                    "memory_limit_bytes": adapter_module.EXPECTED_MEMORY_BYTES,
                }
            ),
            stderr="",
        )

    runtime = adapter_module.DockerRuntime(
        project_dir=tmp_path,
        project_name="verified-project",
        compose_file=compose,
        run_command=fake_run,
    )
    url = "https://www.youtube.com/watch?v=stdinonly01"
    image_id = "sha256:" + "b" * 64

    result = runtime.legacy_container_call(
        "preflight",
        {"case": {"url": url}},
        image_id=image_id,
        source_container_id="a" * 64,
        container_name="ytdlbot-media-evidence-preflight",
        timeout=10,
    )

    command, stdin = commands[-1]
    assert command[:2] == ["/bin/bash", "-c"]
    assert "--env-file <(" in command[2]
    assert "docker inspect" in command[2]
    assert "REDIS_URL=" in command[2]
    assert "WEBHOOK_URL=" in command[2]
    assert "YTDLP_COOKIES_B64=" in command[2]
    for proxy_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        assert f"--env {proxy_name}=" in command[2]
    assert "--tmpfs" in command[2]
    assert "flock -n" in command[2]
    assert image_id in command
    assert url not in " ".join(command)
    assert url in (stdin or "")
    assert "uvicorn" not in " ".join(command)
    assert result["safe"] is True


def test_process_matcher_recognizes_baseline_python_ytdlp_binary(
    adapter_module: Any,
) -> None:
    helper = adapter_module._CONTAINER_HELPER
    assert '"/opt/venv/bin/yt-dlp"' in helper


@pytest.mark.parametrize("interruption", ["timeout", "keyboard"])
def test_legacy_interruption_removes_exact_owned_container_before_return(
    adapter_module: Any,
    tmp_path: Path,
    interruption: str,
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services: {bot: {image: example.invalid/bot}}\n", encoding="utf-8"
    )
    container_name = "ytdlbot-media-evidence-deadline"
    state = {"alive": False}
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(commands) == 1:
            state["alive"] = True
            if interruption == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            raise KeyboardInterrupt
        assert state["alive"] is True
        assert container_name in command
        state["alive"] = False
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    runtime = adapter_module.DockerRuntime(
        project_dir=tmp_path,
        project_name="verified-project",
        compose_file=compose,
        run_command=fake_run,
    )

    expected_error = (
        adapter_module.AdapterTimeout if interruption == "timeout" else KeyboardInterrupt
    )
    with pytest.raises(expected_error):
        runtime.legacy_container_call(
            "run-once",
            {"case": {"url": "https://www.youtube.com/watch?v=deadline01"}},
            image_id="sha256:" + "b" * 64,
            source_container_id="a" * 64,
            container_name=container_name,
            timeout=1,
        )

    assert state["alive"] is False
    assert len(commands) == 2


@pytest.mark.parametrize("cleanup_failure", ["exit", "timeout"])
def test_legacy_timeout_cleanup_failure_is_fail_closed(
    adapter_module: Any,
    tmp_path: Path,
    cleanup_failure: str,
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services: {bot: {image: example.invalid/bot}}\n", encoding="utf-8"
    )
    container_name = "ytdlbot-media-evidence-cleanup-failure"
    state = {"alive": False}
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(commands) == 1:
            state["alive"] = True
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if cleanup_failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 76, stdout="", stderr="secret")

    runtime = adapter_module.DockerRuntime(
        project_dir=tmp_path,
        project_name="verified-project",
        compose_file=compose,
        run_command=fake_run,
    )

    with pytest.raises(
        adapter_module.AdapterError, match="cleanup could not prove"
    ) as raised:
        runtime.legacy_container_call(
            "run-once",
            {"case": {"url": "https://www.youtube.com/watch?v=deadline02"}},
            image_id="sha256:" + "b" * 64,
            source_container_id="a" * 64,
            container_name=container_name,
            timeout=1,
        )

    assert state["alive"] is True
    assert len(commands) == 2
    assert "secret" not in str(raised.value)


def _run_cleanup_shell(
    adapter_module: Any,
    tmp_path: Path,
    *,
    docker_mode: str,
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None and os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(candidate) if candidate.is_file() else None
    if bash is None:
        pytest.skip("bash is required for the production cleanup-shell contract")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${FAKE_DOCKER_MODE}" = "daemon-failure" ]; then
  exit 1
fi
if [ "$1" = "ps" ]; then
  [ "$2" = "-aq" ]
  [ "$3" = "--filter" ]
  [ "$4" = "name=^/ytdlbot-media-evidence-proof$" ]
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    flock = fake_bin / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    flock.chmod(0o755)
    if os.name == "nt":
        converted = subprocess.run(
            [bash, "-lc", 'cygpath -u "$1"', "--", str(fake_bin)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        lock_path = subprocess.run(
            [bash, "-lc", 'cygpath -u "$1"', "--", str(tmp_path / "proof.lock")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    else:
        converted = str(fake_bin)
        lock_path = str(tmp_path / "proof.lock")
    env = {
        **os.environ,
        "FAKE_DOCKER_MODE": docker_mode,
        "PATH": f"{converted}:/usr/bin:/bin",
    }
    return subprocess.run(
        [
            bash,
            "-c",
            adapter_module._LEGACY_CLEANUP_SHELL,
            "--",
            lock_path,
            "ytdlbot-media-evidence-proof",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=5,
        env=env,
    )


def test_cleanup_docker_daemon_failure_is_not_treated_as_absence(
    adapter_module: Any,
    tmp_path: Path,
) -> None:
    result = _run_cleanup_shell(
        adapter_module, tmp_path, docker_mode="daemon-failure"
    )

    assert result.returncode != 0


def test_cleanup_successful_empty_exact_name_query_confirms_absence(
    adapter_module: Any,
    tmp_path: Path,
) -> None:
    result = _run_cleanup_shell(adapter_module, tmp_path, docker_mode="absent")

    assert result.returncode == 0
