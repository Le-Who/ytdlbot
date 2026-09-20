#!/usr/bin/env python3
"""Trusted Docker adapter for production media acceptance collection.

All Telegram credentials and the administrative chat identifier are read and
used by a helper running inside the existing bot container. The host process
receives only bounded counters, closed outcomes, and sanitized capability data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

EXPECTED_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
PHASES = ("resolve", "first_byte", "materialize", "deliver")
SAFE_RESULTS = {"success", "delivered", "failed", "error", "uncertain", "partial"}
TERMINAL_JOBS = {"completed", "failed"}
PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
CONTAINER_RE = re.compile(r"[0-9a-f]{12,64}")
RELEASE_RE = re.compile(r"[0-9a-f]{40}")
PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class AdapterError(RuntimeError):
    """An operator-safe error; raw Docker output must never enter its message."""


class AdapterTimeout(TimeoutError):
    """The bounded observation deadline expired."""


_CONTAINER_HELPER = r"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

action = sys.argv[1]
payload = json.loads(sys.stdin.read() or "{}")

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")))

def http_json(path, *, timeout=5.0):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=timeout) as response:
        return response.status, json.loads(response.read())

def read_counter(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return 0

def cpu_seconds():
    try:
        values = {}
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
            key, value = line.split(None, 1)
            values[key] = int(value)
        if "usage_usec" in values:
            return values["usage_usec"] / 1_000_000
        return values.get("usage_nsec", 0) / 1_000_000_000
    except (OSError, ValueError):
        return 0.0

def memory_limit():
    raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    return 0 if raw == "max" else int(raw)

def process_rss_bytes():
    total = 0
    try:
        pids = Path("/sys/fs/cgroup/cgroup.procs").read_text().splitlines()
    except OSError:
        return 0
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            lines = Path("/proc").joinpath(pid, "status").read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("VmRSS:"):
                try:
                    total += int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
                break
    return total

def integer_environment(name):
    try:
        return int(os.environ.get(name, "0") or 0)
    except ValueError:
        return 0

def labels(raw):
    return dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', raw))

def metric_rows(text, metric):
    result = []
    pattern = re.compile(r"^" + re.escape(metric) + r'(?:\{([^}]*)\})?\s+([-+0-9.eE]+)$')
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            result.append((labels(match.group(1) or ""), float(match.group(2))))
    return result

def metric_sum(text, metric, **required):
    return sum(
        value
        for item_labels, value in metric_rows(text, metric)
        if all(item_labels.get(key) == expected for key, expected in required.items())
    )

def metrics_snapshot():
    with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=5.0) as response:
        text = response.read().decode("utf-8", "replace")
    phases = {
        phase: {
            "count": int(metric_sum(
                text,
                "ytdlbot_media_pipeline_duration_seconds_count",
                phase=phase,
                platform="youtube",
            )),
            "sum": metric_sum(
                text,
                "ytdlbot_media_pipeline_duration_seconds_sum",
                phase=phase,
                platform="youtube",
            ),
        }
        for phase in ("resolve", "first_byte", "materialize", "deliver")
    }
    results = {}
    for item_labels, value in metric_rows(text, "ytdlbot_media_pipeline_results_total"):
        if item_labels.get("platform") == "youtube" and item_labels.get("status"):
            results[item_labels["status"]] = int(value)
    providers = {}
    for item_labels, value in metric_rows(text, "ytdlbot_media_provider_wins_total"):
        if item_labels.get("platform") == "youtube" and item_labels.get("provider"):
            providers[item_labels["provider"]] = int(value)
    provider_attempts = {}
    for item_labels, value in metric_rows(text, "ytdlbot_media_provider_attempts_total"):
        if item_labels.get("platform") == "youtube" and item_labels.get("provider"):
            provider_attempts[item_labels["provider"]] = int(value)
    legacy = {
        name: {
            "count": int(metric_sum(text, name + "_count")),
            "sum": metric_sum(text, name + "_sum"),
        }
        for name in (
            "ytdlbot_extraction_duration_seconds",
            "ytdlbot_download_duration_seconds",
            "ytdlbot_conversion_duration_seconds",
            "ytdlbot_upload_duration_seconds",
        )
    }
    legacy_results = {
        "success": int(metric_sum(text, "ytdlbot_downloads_success_total")),
        "failed": int(metric_sum(text, "ytdlbot_downloads_failed_total")),
    }
    return {
        "phases": phases,
        "results": results,
        "wasted_bytes": int(metric_sum(
            text, "ytdlbot_media_race_wasted_bytes_total", platform="youtube"
        )),
        "file_id_hits": int(metric_sum(
            text,
            "ytdlbot_media_cache_events_total",
            event="file_id_hit",
            platform="youtube",
        )),
        "providers": providers,
        "provider_attempts": provider_attempts,
        "active_downloads": int(metric_sum(text, "ytdlbot_active_downloads")),
        "queue_depth": int(metric_sum(text, "ytdlbot_media_queue_depth")),
        "legacy": legacy,
        "legacy_results": legacy_results,
        "required_metrics_present": all(name in text for name in (
            "ytdlbot_media_pipeline_duration_seconds",
            "ytdlbot_media_pipeline_results_total",
            "ytdlbot_media_race_wasted_bytes_total",
            "ytdlbot_media_provider_wins_total",
        )),
        "legacy_metrics_present": all(name in text for name in (
            "ytdlbot_extraction_duration_seconds",
            "ytdlbot_download_duration_seconds",
            "ytdlbot_upload_duration_seconds",
        )),
        "active_downloads_present": "ytdlbot_active_downloads" in text,
        "queue_depth_present": "ytdlbot_media_queue_depth" in text,
    }

def classify_failure(raw):
    lowered = (raw or "").lower()
    failures = []
    if "429" in lowered:
        failures.append({"status": 429, "cause": "rate-limited"})
    if "403" in lowered:
        if "expired" in lowered or "signature" in lowered:
            cause = "expired-signature"
        elif "geo" in lowered:
            cause = "geo-blocked"
        elif "bot" in lowered or "challenge" in lowered:
            cause = "bot-detection"
        elif "policy" in lowered:
            cause = "upstream-policy"
        elif "forbidden" in lowered:
            cause = "forbidden"
        else:
            cause = "unknown"
        failures.append({"status": 403, "cause": cause})
    if "resolve" in lowered or "extract" in lowered:
        stage = "resolve"
    elif "first byte" in lowered or "first_byte" in lowered:
        stage = "first_byte"
    elif "download" in lowered or "ffmpeg" in lowered or "material" in lowered:
        stage = "materialize"
    elif "telegram" in lowered or "deliver" in lowered or "send" in lowered:
        stage = "deliver"
    else:
        stage = "validation"
    return failures, stage

def job_snapshot(update_id, since):
    path = Path(os.environ.get("YTDLBOT_JOB_DB", "/srv/ytdlbot/state/jobs.sqlite3"))
    if not path.is_file():
        return {"supported": False, "state": None, "deliveries": [], "other_jobs": 0, "active_jobs": 0}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"jobs", "deliveries"}.issubset(tables):
            return {"supported": False, "state": None, "deliveries": [], "other_jobs": 0, "active_jobs": 0}
        row = connection.execute(
            "SELECT job_id, state, error FROM jobs WHERE update_id = ?", (update_id,)
        ).fetchone()
        other_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE accepted_at >= ? AND update_id != ?",
            (since, update_id),
        ).fetchone()[0]
        active_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE state IN ('accepted', 'running', 'checkpointed')"
        ).fetchone()[0]
        deliveries = []
        if row is not None:
            deliveries = [
                {
                    "outcome": item["outcome"],
                    "finalized": bool(item["finalized"]),
                    "response_confirmed": item["delivery_id"] is not None,
                }
                for item in connection.execute(
                    "SELECT outcome, finalized, delivery_id FROM deliveries "
                    "WHERE job_id = ? AND item_key != '__job__'",
                    (row["job_id"],),
                ).fetchall()
            ]
        http_failures, failure_stage = classify_failure(
            row["error"] if row is not None else None
        )
        return {
            "supported": True,
            "state": row["state"] if row is not None else None,
            "deliveries": deliveries,
            "other_jobs": int(other_jobs),
            "active_jobs": int(active_jobs),
            "http_failures": http_failures,
            "failure_stage": failure_stage if row is not None and row["state"] == "failed" else None,
        }
    except (OSError, sqlite3.Error):
        return {"supported": False, "state": None, "deliveries": [], "other_jobs": 0, "active_jobs": 0}
    finally:
        try:
            connection.close()
        except Exception:
            pass

def validate_template(update):
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    entities = message.get("entities")
    return (
        isinstance(update.get("update_id"), int)
        and isinstance(message.get("message_id"), int)
        and isinstance(message.get("date"), int)
        and isinstance(message.get("text"), str)
        and message["text"].startswith("/mp4 https://")
        and entities == [{"type": "bot_command", "offset": 0, "length": 4}]
        and "chat" not in message
        and "from" not in message
    )

def cache_modes():
    modes = ["legacy-inf"]
    try:
        from app.core.media_cache import MediaCache
        from app.services.media.pipeline import build_media_request
        if MediaCache and build_media_request and os.environ.get("BOT_TOKEN"):
            modes.append("media-cache")
    except (ImportError, AttributeError):
        pass
    return modes

if action == "preflight":
    ready_status = None
    ready = {}
    try:
        ready_status, ready = http_json("/health/ready")
    except Exception:
        pass
    health_status, health = http_json("/health")
    metrics = metrics_snapshot()
    sample = payload.get("update") or {
        "update_id": 1000000001,
        "message": {
            "message_id": 1000000002,
            "date": 0,
            "text": "/mp4 https://www.youtube.com/watch?v=preflight00",
            "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
        },
    }
    secrets_ready = all(os.environ.get(name) for name in (
        "BOT_TOKEN", "ADMIN_CHAT_ID", "TELEGRAM_SECRET_TOKEN"
    ))
    emit({
        "candidate_ready": ready_status == 200 and ready.get("ready") is True,
        "legacy_healthy": health_status == 200 and health.get("ok") is True,
        "release": ready.get("release") or os.environ.get("APP_RELEASE") or None,
        "max_media_file_mb": ready.get("max_media_file_mb") or integer_environment(
            "MAX_MEDIA_FILE_MB"
        ),
        "local_bot_api_ready": (
            ready.get("local_bot_api", {}).get("required") is True
            and ready.get("local_bot_api", {}).get("functional_probe") is True
        ),
        "memory_limit_bytes": memory_limit(),
        "rss_bytes": process_rss_bytes(),
        "secrets_available": bool(secrets_ready),
        "webhook_template_valid": validate_template(sample),
        "required_metrics_present": metrics["required_metrics_present"],
        "legacy_metrics_present": metrics["legacy_metrics_present"],
        "active_downloads_present": metrics["active_downloads_present"],
        "queue_depth_present": metrics["queue_depth_present"],
        "job_store_supported": job_snapshot(0, time.time())["supported"],
        "cache_modes": cache_modes(),
        "bounded_cancel_supported": False,
    })
elif action == "snapshot":
    update_id = int(payload["update_id"])
    since = float(payload["since"])
    emit({
        "metrics": metrics_snapshot(),
        "cpu_seconds": cpu_seconds(),
        "rss_bytes": process_rss_bytes(),
        "job": job_snapshot(update_id, since),
    })
elif action == "submit":
    update = payload["update"]
    if not validate_template(update):
        raise SystemExit(11)
    admin_chat_id = int(os.environ["ADMIN_CHAT_ID"])
    secret = os.environ["TELEGRAM_SECRET_TOKEN"]
    message = update["message"]
    message["chat"] = {"id": admin_chat_id, "type": "private"}
    message["from"] = {
        "id": admin_chat_id,
        "is_bot": False,
        "first_name": "Media acceptance",
    }
    body = json.dumps(update, separators=(",", ":")).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8000/webhook",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Correlation-ID": payload["correlation_id"],
            "X-Telegram-Bot-Api-Secret-Token": secret,
        },
    )
    with urllib.request.urlopen(request, timeout=float(payload["timeout_seconds"])) as response:
        response_body = json.loads(response.read())
        emit({"accepted": response.status == 200 and response_body == {"ok": True}})
elif action == "evict-case":
    url = payload["url"]
    deleted = 0
    mode = "legacy-inf"
    async def evict():
        global deleted, mode
        from app.core import state
        redis = state.redis_client
        if redis is None:
            return False
        keys = ["inf:" + url]
        try:
            from app.core.media_cache import MediaCache
            from app.services.media.pipeline import build_media_request
            request = build_media_request(url, kind="video", caller_scope="command")
            cache = MediaCache(redis)
            bot_id = os.environ["BOT_TOKEN"].split(":", 1)[0]
            keys.extend((
                cache.record_key("metadata", request),
                cache.record_key("signed-url", request),
                cache.record_key("file-id", request, bot_id=bot_id, item_index=0),
            ))
            mode = "media-cache"
        except (ImportError, AttributeError, KeyError, ValueError):
            mode = "legacy-inf"
        deleted = int(await redis.delete(*keys))
        return True
    safe = asyncio.run(evict())
    emit({"safe": bool(safe), "mode": mode, "deleted_count": deleted})
elif action == "cancel":
    # No current application API maps an external correlation ID to a running
    # task. Refuse to claim cancellation; the collector will stop the window.
    emit({"cancelled": False})
else:
    raise SystemExit(12)
"""


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class DockerIdentity:
    container_id: str
    image_id: str
    configured_image: str
    memory_limit_bytes: int


class DockerRuntime:
    """Narrow, injectable Docker Compose command boundary."""

    def __init__(
        self,
        *,
        project_dir: Path,
        project_name: str,
        compose_file: Path,
        run_command: RunCommand = subprocess.run,
    ) -> None:
        resolved = project_dir.resolve(strict=True)
        if (
            project_dir.is_symlink()
            or project_dir.absolute() != resolved
            or not resolved.is_dir()
        ):
            raise AdapterError("project directory must be a real directory")
        if not PROJECT_RE.fullmatch(project_name):
            raise AdapterError("invalid Compose project name")
        compose_candidate = (
            compose_file if compose_file.is_absolute() else resolved / compose_file
        )
        if compose_candidate.is_symlink():
            raise AdapterError("compose file must not be a symlink")
        compose = compose_candidate.resolve(strict=True)
        if not compose.is_file() or resolved not in compose.parents:
            raise AdapterError("compose file must be a regular file inside the project")
        self.project_dir = resolved
        self.project_name = project_name
        self.compose_file = compose
        self._run_command = run_command

    def _run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        try:
            result = self._run_command(
                list(command),
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=timeout,
                cwd=self.project_dir,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterTimeout("Docker command exceeded its deadline") from exc
        except OSError as exc:
            raise AdapterError("Docker command could not be started") from exc
        if result.returncode != 0:
            raise AdapterError("Docker command failed")
        return result.stdout.strip()

    def _compose(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            os.fspath(self.project_dir),
            "-p",
            self.project_name,
            "-f",
            os.fspath(self.compose_file),
            *arguments,
        ]

    def identity(self) -> DockerIdentity:
        container_id = self._run(self._compose("ps", "-q", "bot"))
        if not CONTAINER_RE.fullmatch(container_id):
            raise AdapterError("bot container identity is unavailable")
        raw = self._run(["docker", "inspect", container_id])
        try:
            inspect = json.loads(raw)[0]
            running = inspect["State"]["Running"]
            labels = inspect["Config"]["Labels"]
            memory = int(inspect["HostConfig"]["Memory"])
            image_id = str(inspect["Image"])
            configured_image = str(inspect["Config"]["Image"])
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise AdapterError("bot container inspection is invalid") from exc
        if (
            running is not True
            or labels.get("com.docker.compose.project") != self.project_name
        ):
            raise AdapterError(
                "bot container does not belong to the expected running project"
            )
        if labels.get("com.docker.compose.service") != "bot":
            raise AdapterError("resolved container is not the bot service")
        if not image_id.startswith("sha256:"):
            raise AdapterError("bot image identity is not content-addressed")
        return DockerIdentity(container_id, image_id, configured_image, memory)

    def container_call(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        raw = self._run(
            self._compose(
                "exec",
                "-T",
                "bot",
                "python",
                "-c",
                _CONTAINER_HELPER,
                action,
            ),
            input_text=json.dumps(payload, separators=(",", ":")),
            timeout=timeout,
        )
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AdapterError("container helper returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AdapterError("container helper returned an invalid object")
        return cast(dict[str, Any], decoded)

    def correlated_logs(self, *, since: str, correlation_id: str) -> str:
        raw = self._run(
            self._compose(
                "logs", "--no-color", "--tail", "10000", "--since", since, "bot"
            ),
            timeout=15.0,
        )
        # Raw log text is used in memory only and is never returned or printed.
        return "\n".join(line for line in raw.splitlines() if correlation_id in line)


class ProductionDockerAdapter:
    def __init__(
        self,
        runtime: DockerRuntime,
        *,
        expected_release: str,
        expected_image_id: str,
        runtime_profile: str,
        legacy_attestation: Mapping[str, Any] | None = None,
        external_free_providers: frozenset[str] = frozenset(),
        external_configured_providers: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not RELEASE_RE.fullmatch(expected_release):
            raise AdapterError("expected release must be an exact lowercase commit SHA")
        if runtime_profile not in {"legacy-baseline", "candidate"}:
            raise AdapterError("invalid runtime profile")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id):
            raise AdapterError("expected image ID must be an exact sha256 identity")
        if runtime_profile == "legacy-baseline":
            if not isinstance(legacy_attestation, Mapping) or set(
                legacy_attestation
            ) != {
                "deployment_sha",
                "image_id",
            }:
                raise AdapterError(
                    "legacy immutable deployment attestation is required"
                )
            if (
                legacy_attestation.get("deployment_sha") != expected_release
                or legacy_attestation.get("image_id") != expected_image_id
            ):
                raise AdapterError(
                    "legacy attestation does not match expected identity"
                )
        overlap = external_free_providers & external_configured_providers
        if overlap:
            raise AdapterError("a provider cannot belong to two route classes")
        if external_free_providers and external_configured_providers:
            raise AdapterError("one evidence run cannot mix independent route classes")
        if any(
            not PROVIDER_RE.fullmatch(provider)
            for provider in external_free_providers | external_configured_providers
        ):
            raise AdapterError("provider labels must use the bounded metric-label form")
        self.runtime = runtime
        self.expected_release = expected_release
        self.expected_image_id = expected_image_id
        self.runtime_profile = runtime_profile
        self.external_free_providers = external_free_providers
        self.external_configured_providers = external_configured_providers
        self._clock = clock
        self._sleep = sleep

    @staticmethod
    def _sample_update() -> dict[str, Any]:
        return {
            "update_id": 1_000_000_001,
            "message": {
                "message_id": 1_000_000_002,
                "date": 0,
                "text": "/mp4 https://www.youtube.com/watch?v=preflight00",
                "entities": [{"type": "bot_command", "offset": 0, "length": 4}],
            },
        }

    def preflight(self, update: dict[str, Any] | None = None) -> dict[str, Any]:
        identity = self.runtime.identity()
        probe = self.runtime.container_call(
            "preflight", {"update": update or self._sample_update()}, timeout=15.0
        )
        common = {
            "memory_limit_bytes": EXPECTED_MEMORY_BYTES,
            "secrets_available": True,
            "webhook_template_valid": True,
        }
        if any(probe.get(key) != value for key, value in common.items()):
            raise AdapterError("production adapter preflight failed")
        if probe.get("active_downloads_present") is not True:
            raise AdapterError("active-download quiescence metric is unavailable")
        if int(probe.get("rss_bytes") or 0) <= 0:
            raise AdapterError("process RSS probe is unavailable")
        if identity.memory_limit_bytes != EXPECTED_MEMORY_BYTES:
            raise AdapterError("bot container memory limit is not the expected 2 GiB")
        if identity.image_id != self.expected_image_id:
            raise AdapterError("live bot image ID does not match expected image ID")
        if self.runtime_profile == "candidate":
            if not re.fullmatch(
                r"[^\s]+@sha256:[0-9a-f]{64}", identity.configured_image
            ):
                raise AdapterError(
                    "candidate bot image is not pinned by registry digest"
                )
            candidate = {
                "candidate_ready": True,
                "release": self.expected_release,
                "max_media_file_mb": 2000,
                "local_bot_api_ready": True,
                "required_metrics_present": True,
                "job_store_supported": True,
            }
            if any(probe.get(key) != value for key, value in candidate.items()):
                raise AdapterError("candidate readiness contract failed")
            if probe.get("queue_depth_present") is not True:
                raise AdapterError("candidate queue quiescence metric is unavailable")
        else:
            if probe.get("legacy_healthy") is not True:
                raise AdapterError("legacy baseline health check failed")
            if probe.get("legacy_metrics_present") is not True:
                raise AdapterError("legacy baseline metrics are unavailable")
        cache_modes = probe.get("cache_modes")
        required_cache_mode = (
            "media-cache" if self.runtime_profile == "candidate" else "legacy-inf"
        )
        if not isinstance(cache_modes, list) or required_cache_mode not in cache_modes:
            raise AdapterError("exact cache eviction capability is unavailable")
        return {
            "ready": True,
            "runtime_profile": self.runtime_profile,
            "release": probe.get("release") or self.expected_release,
            "health_contract": (
                "/health/ready" if self.runtime_profile == "candidate" else "/health"
            ),
            "max_media_file_mb": int(probe.get("max_media_file_mb") or 0),
            "local_bot_api_ready": probe.get("local_bot_api_ready") is True,
            "job_store_supported": probe.get("job_store_supported") is True,
            "container_id": identity.container_id[:12],
            "image_id": identity.image_id,
            "image_reference": (
                identity.configured_image
                if self.runtime_profile == "candidate"
                else identity.image_id
            ),
            "identity_binding": (
                "candidate-release-and-digest"
                if self.runtime_profile == "candidate"
                else "legacy-deployment-attestation"
            ),
            "current_memory_limit_bytes": identity.memory_limit_bytes,
            "production_ip_attested": True,
            "cache_modes": list(cache_modes),
            "bounded_cancel_supported": probe.get("bounded_cancel_supported") is True,
        }

    def identity(self) -> dict[str, Any]:
        return self.preflight()

    def evict_case(self, payload: Mapping[str, Any]) -> bool:
        self.preflight()
        result = self.runtime.container_call("evict-case", payload, timeout=30.0)
        expected_mode = (
            "media-cache" if self.runtime_profile == "candidate" else "legacy-inf"
        )
        return result.get("safe") is True and result.get("mode") == expected_mode

    @staticmethod
    def _metric_result_delta(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> int:
        keys = set(before) | set(after)
        if any(key not in SAFE_RESULTS for key in keys):
            return -1
        return sum(int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys)

    @staticmethod
    def _terminal(snapshot: Mapping[str, Any], before: Mapping[str, Any]) -> bool:
        job = snapshot.get("job")
        if isinstance(job, dict) and job.get("supported") is True:
            return job.get("state") in TERMINAL_JOBS
        before_metrics = cast(Mapping[str, Any], before["metrics"])
        after_metrics = cast(Mapping[str, Any], snapshot["metrics"])
        return (
            ProductionDockerAdapter._metric_result_delta(
                cast(Mapping[str, Any], before_metrics["results"]),
                cast(Mapping[str, Any], after_metrics["results"]),
            )
            == 1
        )

    def _route_outcome(
        self,
        before_wins: Mapping[str, Any],
        after_wins: Mapping[str, Any],
        before_attempts: Mapping[str, Any],
        after_attempts: Mapping[str, Any],
        correlated_logs: str,
        *,
        cache_hit: bool,
    ) -> dict[str, Any]:
        deltas = {
            provider: int(after_wins.get(provider, 0))
            - int(before_wins.get(provider, 0))
            for provider in set(before_wins) | set(after_wins)
        }
        attempt_deltas = {
            provider: int(after_attempts.get(provider, 0))
            - int(before_attempts.get(provider, 0))
            for provider in set(before_attempts) | set(after_attempts)
        }
        structured_attempts = self._structured_provider_attempts(correlated_logs)
        free = {
            provider
            for provider in self.external_free_providers
            if attempt_deltas.get(provider, 0) > 0 or provider in structured_attempts
        }
        configured = {
            provider
            for provider in self.external_configured_providers
            if attempt_deltas.get(provider, 0) > 0 or provider in structured_attempts
        }
        attempted = bool(free or configured)
        winners = {provider for provider, delta in deltas.items() if delta == 1}
        unclassified_winners = winners - free - configured - {"ytdlp"}
        if unclassified_winners:
            raise AdapterError(
                "winning provider lacks an approved route classification"
            )
        succeeded = bool(winners & (free | configured))
        route_class: str | None = None
        if attempted:
            route_class = "external-free" if free else "external-configured"
        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "route_class": route_class,
        }

    @staticmethod
    def _structured_provider_attempts(correlated_logs: str) -> set[str]:
        attempts: set[str] = set()
        for line in correlated_logs.splitlines():
            try:
                payload = json.loads(line[line.index("{") :])
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if payload.get("event") != "provider-attempt":
                continue
            provider = payload.get("provider")
            if isinstance(provider, str) and PROVIDER_RE.fullmatch(provider):
                attempts.add(provider)
        return attempts

    @staticmethod
    def _exact_downloaded_measurement(
        correlated_logs: str, *, runtime_profile: str
    ) -> dict[str, Any]:
        field = (
            "bytes_downloaded"
            if runtime_profile == "candidate"
            else "delivered_file_size_bytes"
        )
        values: set[int] = set()
        for line in correlated_logs.splitlines():
            try:
                payload = json.loads(line[line.index("{") :])
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            value = payload.get(field)
            allowed_events = (
                {"media-measurement", "delivery-success"}
                if runtime_profile == "candidate"
                else {"delivery", "telegram-delivery"}
            )
            if payload.get("event") not in allowed_events:
                continue
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values.add(value)
        if len(values) != 1:
            return {
                "availability": "unavailable",
                "method": "unavailable",
                "value": None,
            }
        return {
            "availability": "measured",
            "method": (
                "structured-request-bytes"
                if runtime_profile == "candidate"
                else "legacy-correlated-delivered-size"
            ),
            "value": next(iter(values)),
        }

    @staticmethod
    def _metrics_idle(metrics: Mapping[str, Any]) -> bool:
        return (
            int(metrics.get("active_downloads", -1)) == 0
            and int(metrics.get("queue_depth", -1)) == 0
        )

    @staticmethod
    def _activity_signature(snapshot: Mapping[str, Any]) -> str:
        metrics = cast(Mapping[str, Any], snapshot["metrics"])
        observed = {
            key: metrics.get(key)
            for key in (
                "phases",
                "results",
                "wasted_bytes",
                "file_id_hits",
                "providers",
                "provider_attempts",
                "legacy",
                "legacy_results",
            )
        }
        return json.dumps(observed, sort_keys=True, separators=(",", ":"))

    def _quiescent_snapshot(
        self, request: Mapping[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        first = self.runtime.container_call(
            "snapshot", request, timeout=min(15.0, max(0.01, deadline - self._clock()))
        )
        first_metrics = cast(Mapping[str, Any], first["metrics"])
        first_job = cast(Mapping[str, Any], first["job"])
        if not self._metrics_idle(first_metrics) or (
            self.runtime_profile == "candidate" and first_job.get("active_jobs") != 0
        ):
            raise AdapterError("media runtime is not quiescent")
        self._sleep(min(0.25, max(0.01, deadline - self._clock())))
        second = self.runtime.container_call(
            "snapshot", request, timeout=min(15.0, max(0.01, deadline - self._clock()))
        )
        second_metrics = cast(Mapping[str, Any], second["metrics"])
        second_job = cast(Mapping[str, Any], second["job"])
        if (
            not self._metrics_idle(second_metrics)
            or (
                self.runtime_profile == "candidate"
                and second_job.get("active_jobs") != 0
            )
            or self._activity_signature(first) != self._activity_signature(second)
        ):
            raise AdapterError("media runtime did not remain quiescent for quiet grace")
        return second

    @staticmethod
    def _http_failures(correlated_logs: str) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        lower = correlated_logs.lower()
        for status in (403, 429):
            if str(status) not in lower:
                continue
            if status == 429:
                cause = "rate-limited"
            elif "expired" in lower or "signature" in lower:
                cause = "expired-signature"
            elif "geo" in lower:
                cause = "geo-blocked"
            elif "bot" in lower or "challenge" in lower:
                cause = "bot-detection"
            elif "policy" in lower:
                cause = "upstream-policy"
            elif "forbidden" in lower:
                cause = "forbidden"
            else:
                cause = "unknown"
            failures.append({"status": status, "cause": cause})
        return failures

    @staticmethod
    def _legacy_first_byte_seconds(
        correlated_logs: str, started: float
    ) -> float | None:
        for line in correlated_logs.splitlines():
            lowered = line.lower()
            if "first_byte" not in lowered and "first byte" not in lowered:
                continue
            try:
                event = json.loads(line[line.index("{") :])
                raw_timestamp = event.get("timestamp") or event.get("time")
                if isinstance(raw_timestamp, (int, float)):
                    return max(0.0, float(raw_timestamp) - started)
                if isinstance(raw_timestamp, str):
                    observed = datetime.fromisoformat(
                        raw_timestamp.replace("Z", "+00:00")
                    ).timestamp()
                    return max(0.0, observed - started)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return None

    @staticmethod
    def _legacy_delivery_outcome(correlated_logs: str) -> str | None:
        lowered = correlated_logs.lower()
        method_seen = "sendvideo" in lowered or "senddocument" in lowered
        if not method_seen:
            return None
        success_seen = (
            "telegram-success" in lowered
            or "status=200" in lowered
            or '"ok":true' in lowered
        )
        if success_seen:
            return "success"
        failure_seen = (
            "telegram-failed" in lowered
            or "telegram-error" in lowered
            or "send-failed" in lowered
        )
        return "failed" if failure_seen else None

    @staticmethod
    def _legacy_delivery_confirmed(correlated_logs: str) -> bool:
        return (
            ProductionDockerAdapter._legacy_delivery_outcome(correlated_logs)
            == "success"
        )

    @staticmethod
    def _legacy_phase_metrics(
        before_metrics: Mapping[str, Any],
        after_metrics: Mapping[str, Any],
        *,
        first_byte_seconds: float | None,
    ) -> tuple[dict[str, dict[str, float | int]], bool]:
        before = cast(Mapping[str, Any], before_metrics["legacy"])
        after = cast(Mapping[str, Any], after_metrics["legacy"])

        def delta(name: str) -> tuple[int, float]:
            return (
                int(after[name]["count"]) - int(before[name]["count"]),
                float(after[name]["sum"]) - float(before[name]["sum"]),
            )

        resolve_count, resolve_sum = delta("ytdlbot_extraction_duration_seconds")
        download_count, download_sum = delta("ytdlbot_download_duration_seconds")
        conversion_count, conversion_sum = delta("ytdlbot_conversion_duration_seconds")
        deliver_count, deliver_sum = delta("ytdlbot_upload_duration_seconds")
        materialize_count = download_count + conversion_count
        valid = (
            resolve_count in {0, 1}
            and download_count in {0, 1}
            and conversion_count in {0, 1}
            and deliver_count in {0, 1}
            and min(resolve_sum, download_sum, conversion_sum, deliver_sum) >= 0
        )
        phase_metrics = {
            "resolve": {
                "before_count": 0,
                "before_sum": 0.0,
                "after_count": 1 if resolve_count == 1 else 0,
                "after_sum": max(0.0, resolve_sum),
            },
            "first_byte": {
                "before_count": 0,
                "before_sum": 0.0,
                "after_count": 1 if first_byte_seconds is not None else 0,
                "after_sum": first_byte_seconds or 0.0,
            },
            "materialize": {
                "before_count": 0,
                "before_sum": 0.0,
                "after_count": 1 if materialize_count >= 1 else 0,
                "after_sum": max(0.0, download_sum + conversion_sum),
            },
            "deliver": {
                "before_count": 0,
                "before_sum": 0.0,
                "after_count": 1 if deliver_count == 1 else 0,
                "after_sum": max(0.0, deliver_sum),
            },
        }
        return phase_metrics, valid

    def observe(
        self,
        *,
        update: dict[str, Any],
        correlation_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.preflight(update)
        started = self._clock()
        deadline = started + timeout_seconds
        log_since = datetime.fromtimestamp(started, tz=UTC).isoformat()
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            raise AdapterError("webhook update ID is invalid")
        request = {"update_id": update_id, "since": started}
        before = self._quiescent_snapshot(request, deadline=deadline)
        submit_timeout = max(1.0, deadline - self._clock())
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="evidence-submit"
        )
        submit_future = executor.submit(
            self.runtime.container_call,
            "submit",
            {
                "update": update,
                "correlation_id": correlation_id,
                "timeout_seconds": submit_timeout,
            },
            timeout=submit_timeout + 2.0,
        )
        rss_samples = [int(before["rss_bytes"])]
        latest = before
        logs = ""
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise AdapterTimeout("case observation exceeded its deadline")
                latest = self.runtime.container_call(
                    "snapshot", request, timeout=min(15.0, remaining)
                )
                rss_samples.append(int(latest["rss_bytes"]))
                logs = self.runtime.correlated_logs(
                    since=log_since, correlation_id=correlation_id
                )
                if self.runtime_profile == "legacy-baseline":
                    legacy_outcome = self._legacy_delivery_outcome(logs)
                    before_legacy = cast(Mapping[str, Any], before["metrics"])["legacy"]
                    after_legacy = cast(Mapping[str, Any], latest["metrics"])["legacy"]
                    upload_delta = int(
                        after_legacy["ytdlbot_upload_duration_seconds"]["count"]
                    ) - int(before_legacy["ytdlbot_upload_duration_seconds"]["count"])
                    before_legacy_results = cast(Mapping[str, Any], before["metrics"])[
                        "legacy_results"
                    ]
                    after_legacy_results = cast(Mapping[str, Any], latest["metrics"])[
                        "legacy_results"
                    ]
                    legacy_result_delta = sum(
                        int(after_legacy_results.get(label, 0))
                        - int(before_legacy_results.get(label, 0))
                        for label in ("success", "failed")
                    )
                    terminal = (
                        legacy_outcome is not None
                        and legacy_result_delta == 1
                        and (legacy_outcome == "failed" or upload_delta == 1)
                    )
                else:
                    terminal = self._terminal(latest, before)
                if terminal:
                    break
                self._sleep(min(0.25, max(0.01, remaining)))
            try:
                submit = submit_future.result(
                    timeout=max(0.01, deadline - self._clock())
                )
            except TimeoutError as exc:
                raise AdapterTimeout(
                    "webhook submission exceeded its deadline"
                ) from exc
            if submit != {"accepted": True}:
                raise AdapterError("webhook was not durably accepted")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        logs = self.runtime.correlated_logs(
            since=log_since, correlation_id=correlation_id
        )
        before_metrics = cast(Mapping[str, Any], before["metrics"])
        after_metrics = cast(Mapping[str, Any], latest["metrics"])
        job_state: str | None
        if self.runtime_profile == "legacy-baseline":
            legacy_outcome = self._legacy_delivery_outcome(logs)
            phase_metrics, legacy_phases_valid = self._legacy_phase_metrics(
                before_metrics,
                after_metrics,
                first_byte_seconds=self._legacy_first_byte_seconds(logs, started),
            )
        else:
            legacy_phases_valid = True
            phase_metrics = {
                phase: {
                    "before_count": int(before_metrics["phases"][phase]["count"]),
                    "before_sum": float(before_metrics["phases"][phase]["sum"]),
                    "after_count": int(after_metrics["phases"][phase]["count"]),
                    "after_sum": float(after_metrics["phases"][phase]["sum"]),
                }
                for phase in PHASES
            }
            raw_first_byte_delta = (
                phase_metrics["first_byte"]["after_count"]
                - phase_metrics["first_byte"]["before_count"]
            )
        before_hits = int(before_metrics["file_id_hits"])
        after_hits = int(after_metrics["file_id_hits"])
        bypassed = []
        if after_hits - before_hits == 1:
            bypassed = [
                phase
                for phase in ("resolve", "first_byte", "materialize")
                if phase_metrics[phase]["after_count"]
                == phase_metrics[phase]["before_count"]
            ]
        unavailable_phases: list[str] = []
        if "first_byte" in bypassed:
            first_byte_measurement = {
                "availability": "measured",
                "method": "file-id-cache",
            }
        elif self.runtime_profile == "legacy-baseline":
            if self._legacy_first_byte_seconds(logs, started) is None:
                unavailable_phases.append("first_byte")
                first_byte_measurement = {
                    "availability": "unavailable",
                    "method": "unavailable",
                }
            else:
                first_byte_measurement = {
                    "availability": "measured",
                    "method": "legacy-correlated-progress",
                }
        elif raw_first_byte_delta == 1:
            first_byte_measurement = {
                "availability": "measured",
                "method": "task11-metric",
            }
        else:
            unavailable_phases.append("first_byte")
            first_byte_measurement = {
                "availability": "unavailable",
                "method": "unavailable",
            }
        if "first_byte" in unavailable_phases:
            phase_metrics["first_byte"] = {
                **phase_metrics["first_byte"],
            }
        downloaded_measurement = (
            {
                "availability": "measured",
                "method": "file-id-cache",
                "value": 0,
            }
            if after_hits - before_hits == 1
            else self._exact_downloaded_measurement(
                logs, runtime_profile=self.runtime_profile
            )
        )
        job = cast(Mapping[str, Any], latest["job"])
        deliveries = job.get("deliveries", [])
        confirmed_deliveries = [
            item
            for item in deliveries
            if isinstance(item, dict)
            and item.get("finalized") is True
            and item.get("response_confirmed") is True
        ]
        if self.runtime_profile == "legacy-baseline":
            legacy_delivery = self._legacy_delivery_confirmed(logs)
            delivery_outcomes = ["success" if legacy_delivery else "failed"]
            before_results: Mapping[str, Any] = {"success": 0}
            after_results: Mapping[str, Any] = {
                "success": 1 if legacy_delivery else 0,
                "failed": 0 if legacy_delivery else 1,
            }
        elif deliveries:
            delivery_outcomes = [str(item["outcome"]) for item in deliveries]
            before_results = cast(Mapping[str, Any], before_metrics["results"])
            after_results = cast(Mapping[str, Any], after_metrics["results"])
        else:
            result_delta = {
                key: int(after_metrics["results"].get(key, 0))
                - int(before_metrics["results"].get(key, 0))
                for key in set(before_metrics["results"])
                | set(after_metrics["results"])
            }
            delivery_outcomes = [
                key for key, delta in result_delta.items() if delta == 1
            ]
            before_results = cast(Mapping[str, Any], before_metrics["results"])
            after_results = cast(Mapping[str, Any], after_metrics["results"])
        if self.runtime_profile == "legacy-baseline":
            successful = (
                legacy_delivery
                and legacy_phases_valid
                and all(
                    phase_metrics[phase]["after_count"] == 1
                    for phase in ("resolve", "materialize", "deliver")
                )
            )
            job_state = "completed" if successful else "failed"
        else:
            successful = (
                job.get("state") == "completed"
                and bool(deliveries)
                and len(confirmed_deliveries) == len(deliveries)
                and all(item.get("outcome") == "success" for item in deliveries)
            )
            job_state = job.get("state")
        events = ["webhook-accepted"]
        events.append("job-completed" if job_state == "completed" else "job-failed")
        if successful:
            events.append("delivery-success")
        elif any(item.get("outcome") == "uncertain" for item in deliveries):
            events.append("delivery-uncertain")
        else:
            events.append("delivery-failed")
        http_failures = self._http_failures(logs)
        for failure in job.get("http_failures", []):
            if failure not in http_failures:
                http_failures.append(failure)
        return {
            "attribution_confirmed": (
                (
                    legacy_outcome in {"success", "failed"}
                    and legacy_phases_valid
                    and self._metric_result_delta(
                        cast(Mapping[str, Any], before_metrics["legacy_results"]),
                        cast(Mapping[str, Any], after_metrics["legacy_results"]),
                    )
                    == 1
                    and self._metrics_idle(after_metrics)
                )
                if self.runtime_profile == "legacy-baseline"
                else (
                    job.get("other_jobs") == 0
                    and job.get("active_jobs") == 0
                    and self._metrics_idle(after_metrics)
                    and self._metric_result_delta(before_results, after_results) == 1
                )
            ),
            "bypassed_phases": bypassed,
            "unavailable_phases": unavailable_phases,
            "file_id_hits_before": before_hits,
            "file_id_hits_after": after_hits,
            "phase_metrics": phase_metrics,
            "pipeline_results_before": before_results,
            "pipeline_results_after": after_results,
            "wasted_bytes_before": int(before_metrics["wasted_bytes"]),
            "wasted_bytes_after": int(after_metrics["wasted_bytes"]),
            "measurement": {
                "first_byte": first_byte_measurement,
                "downloaded_bytes": downloaded_measurement,
            },
            "cpu_seconds_before": float(before["cpu_seconds"]),
            "cpu_seconds_after": float(latest["cpu_seconds"]),
            "rss_bytes_samples": rss_samples,
            "job_state": job_state,
            "delivery_outcomes": delivery_outcomes,
            "http_failures": http_failures,
            "failure_stage": job.get("failure_stage"),
            "independent_route": self._route_outcome(
                cast(Mapping[str, Any], before_metrics["providers"]),
                cast(Mapping[str, Any], after_metrics["providers"]),
                cast(Mapping[str, Any], before_metrics["provider_attempts"]),
                cast(Mapping[str, Any], after_metrics["provider_attempts"]),
                logs,
                cache_hit=after_hits - before_hits == 1,
            ),
            "correlated_events": events,
        }

    def cancel(self, correlation_id: str) -> bool:
        self.runtime.identity()
        result = self.runtime.container_call(
            "cancel", {"correlation_id": correlation_id}, timeout=15.0
        )
        return result == {"cancelled": True}


def _input() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise AdapterError("adapter input is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AdapterError("adapter input must be a JSON object")
    return cast(dict[str, Any], payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--compose-file", type=Path, default=Path("docker-compose.yml"))
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--legacy-attestation", type=Path)
    parser.add_argument(
        "--runtime-profile",
        choices=("legacy-baseline", "candidate"),
        required=True,
    )
    parser.add_argument("--external-free-provider", action="append", default=[])
    parser.add_argument("--external-configured-provider", action="append", default=[])
    parser.add_argument(
        "action", choices=("preflight", "identity", "evict-case", "observe", "cancel")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _input()
        runtime = DockerRuntime(
            project_dir=args.project_dir,
            project_name=args.project_name,
            compose_file=args.compose_file,
        )
        legacy_attestation: Mapping[str, Any] | None = None
        if args.legacy_attestation is not None:
            if (
                args.legacy_attestation.is_symlink()
                or not args.legacy_attestation.is_file()
            ):
                raise AdapterError("legacy attestation must be a regular file")
            decoded_attestation = json.loads(
                args.legacy_attestation.read_text(encoding="utf-8")
            )
            if not isinstance(decoded_attestation, dict):
                raise AdapterError("legacy attestation must be a JSON object")
            legacy_attestation = decoded_attestation
        adapter = ProductionDockerAdapter(
            runtime,
            expected_release=args.expected_release,
            expected_image_id=args.expected_image_id,
            runtime_profile=args.runtime_profile,
            legacy_attestation=legacy_attestation,
            external_free_providers=frozenset(args.external_free_provider),
            external_configured_providers=frozenset(args.external_configured_provider),
        )
        if args.action == "preflight":
            output: Any = adapter.preflight(payload.get("update"))
        elif args.action == "identity":
            output = adapter.identity()
        elif args.action == "evict-case":
            output = {"safe": adapter.evict_case(payload)}
        elif args.action == "observe":
            output = adapter.observe(
                update=payload["update"],
                correlation_id=payload["correlation_id"],
                timeout_seconds=float(payload["timeout_seconds"]),
            )
        else:
            output = {"cancelled": adapter.cancel(str(payload["correlation_id"]))}
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    except (AdapterError, AdapterTimeout, KeyError, TypeError, ValueError) as exc:
        # Never print subprocess output or input fields; both may contain secrets.
        print(f"media acceptance adapter failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
