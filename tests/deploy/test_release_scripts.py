from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASH = next(
    path
    for path in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    )
    if path.exists()
)
RELEASE_SHA = "a" * 40
PREVIOUS_RELEASE = "b" * 40
BOT_IMAGE = "ghcr.io/example/ytdlbot@sha256:" + "c" * 64
PREVIOUS_IMAGE = "sha256:" + "d" * 64


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.removesuffix(":").lower()
    tail = resolved.as_posix().split(":", 1)[1]
    return f"/{drive}{tail}"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _activation_workflow_script() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["deploy"]["steps"]
    return next(
        step["with"]["script"]
        for step in steps
        if step.get("name") == "Activate immutable release over verified SSH"
    )


def _write_uploaded_release(path: Path, activation_count: Path) -> None:
    scripts = path / "scripts"
    scripts.mkdir(parents=True)
    compose = "services:\n  bot:\n    image: ${BOT_IMAGE}\n"
    (path / "docker-compose.yml").write_text(compose, encoding="utf-8", newline="\n")
    compose_sha = hashlib.sha256(compose.encode()).hexdigest()
    (path / "release.manifest").write_text(
        "\n".join(
            (
                f"RELEASE_SHA={RELEASE_SHA}",
                f"BOT_IMAGE={BOT_IMAGE}",
                "COMPOSE_PROJECT=verified-project",
                f"COMPOSE_SHA256={compose_sha}",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_executable(
        scripts / "deploy-release.sh",
        f"""#!/bin/sh
set -eu
count_file='{_bash_path(activation_count)}'
count=0
if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi
count=$((count + 1))
printf '%s' "$count" > "$count_file"
if [ "${{FAKE_FAIL_FIRST_ACTIVATION:-0}}" = 1 ] && [ "$count" -eq 1 ]; then
  exit 42
fi
""",
    )
    for name in (
        "bootstrap-production.sh",
        "preflight-production.sh",
        "rollback-release.sh",
    ):
        _write_executable(scripts / name, "#!/bin/sh\nexit 0\n")


@dataclass
class FakeHost:
    root: Path
    release_dir: Path
    fake_bin: Path
    state_dir: Path
    command_log: Path
    runtime_release: Path
    runtime_image: Path
    old_compose: str
    env: dict[str, str]

    def run(self, script_name: str = "deploy-release.sh", **overrides: str):
        env = self.env | overrides
        script = ROOT / "scripts" / script_name
        if not script.exists():
            return subprocess.CompletedProcess(
                [str(script)], 127, "", f"missing script: {script}"
            )
        return subprocess.run(
            [str(BASH), _bash_path(script)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def commands(self) -> list[str]:
        if not self.command_log.exists():
            return []
        return self.command_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def fake_host(tmp_path: Path) -> FakeHost:
    root = tmp_path / "opt" / "ytdlbot"
    release_dir = root / "releases" / RELEASE_SHA
    script_dir = release_dir / "scripts"
    fake_bin = tmp_path / "fake-bin"
    state_dir = tmp_path / "fake-state"
    for directory in (root, script_dir, fake_bin, state_dir):
        directory.mkdir(parents=True, exist_ok=True)
    deploy_state = root / ".deploy"
    deploy_state.mkdir()

    old_compose = "services:\n  bot:\n    image: ${BOT_IMAGE}\n"
    candidate_compose = (
        "services:\n"
        "  bot:\n"
        "    image: ${BOT_IMAGE:?immutable image required}\n"
        "    environment:\n"
        "      - APP_RELEASE=${APP_RELEASE}\n"
    )
    (root / "docker-compose.yml").write_text(old_compose, encoding="utf-8")
    (root / ".env").write_text("BOT_TOKEN=keep-secret\n", encoding="utf-8")
    (release_dir / "docker-compose.yml").write_text(candidate_compose, encoding="utf-8")
    compose_sha = hashlib.sha256(
        (release_dir / "docker-compose.yml").read_bytes()
    ).hexdigest()
    (release_dir / "release.manifest").write_text(
        "\n".join(
            (
                f"RELEASE_SHA={RELEASE_SHA}",
                f"BOT_IMAGE={BOT_IMAGE}",
                "COMPOSE_PROJECT=verified-project",
                f"COMPOSE_SHA256={compose_sha}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (deploy_state / "bootstrap.manifest").write_text(
        "\n".join(
            (
                "BOOTSTRAP_SCHEMA=1",
                "COMPOSE_PROJECT=verified-project",
                "MEDIA_VOLUME=verified-project_media",
                "STATE_VOLUME=verified-project_state",
                "TG_API_VOLUME=verified-project_tg-api-data",
                "REDIS_VOLUME=verified-project_redis-data",
                "BOT_UID_GID=10001:10001",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    for name in (
        "bootstrap-production.sh",
        "deploy-release.sh",
        "preflight-production.sh",
        "rollback-release.sh",
    ):
        source = ROOT / "scripts" / name
        target = script_dir / name
        target.write_bytes(source.read_bytes() if source.exists() else b"#!/bin/sh\n")

    command_log = state_dir / "commands.log"
    runtime_release = state_dir / "runtime-release"
    runtime_image = state_dir / "runtime-image"
    runtime_release.write_text(PREVIOUS_RELEASE, encoding="utf-8")
    runtime_image.write_text(PREVIOUS_IMAGE, encoding="utf-8")

    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
printf 'docker' >> "$FAKE_COMMAND_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$FAKE_COMMAND_LOG"; done
printf '\n' >> "$FAKE_COMMAND_LOG"

if [ "$1" = pull ]; then
  [ "${FAKE_PULL_FAILURE:-0}" != 1 ]
  exit
fi

if [ "$1" = inspect ]; then
  case "$*" in
    *com.docker.compose.project.working_dir*bot-id) printf '%s\n' "${FAKE_PROJECT_WORKING_DIR:-$PROJECT_ROOT}" ;;
    *com.docker.compose.project*bot-id) printf '%s|bot\n' "$COMPOSE_PROJECT" ;;
    *com.docker.compose.project*redis-id) printf '%s|redis\n' "$COMPOSE_PROJECT" ;;
    *com.docker.compose.project*tg-api-id) printf '%s|tg-api\n' "$COMPOSE_PROJECT" ;;
    *com.docker.compose.project*volume-init-id) printf '%s|volume-init\n' "$COMPOSE_PROJECT" ;;
    *State.Status*volume-init-id) printf 'exited|0\n' ;;
    *Config.Image*bot-id) cat "$FAKE_RUNTIME_IMAGE" ;;
    *'{{.Image}}'*bot-id) cat "$FAKE_RUNTIME_IMAGE" ;;
    *Config.Env*bot-id) printf 'APP_RELEASE=%s\n' "$(cat "$FAKE_RUNTIME_RELEASE")" ;;
    *'/srv/ytdlbot/media'*bot-id) printf '%s\n' "${FAKE_BOT_MEDIA_MOUNT:-${COMPOSE_PROJECT}_media|true}" ;;
    *'/srv/ytdlbot/state'*bot-id) printf '%s\n' "${FAKE_BOT_STATE_MOUNT:-${COMPOSE_PROJECT}_state|true}" ;;
    *'/srv/ytdlbot/media'*tg-api-id) printf '%s\n' "${FAKE_TG_MEDIA_MOUNT:-${COMPOSE_PROJECT}_media|false}" ;;
    *'/var/lib/telegram-bot-api'*tg-api-id) printf '%s\n' "${FAKE_TG_SESSION_MOUNT:-${COMPOSE_PROJECT}_tg-api-data|true}" ;;
    *'/data'*redis-id) printf '%s\n' "${FAKE_REDIS_MOUNT:-${COMPOSE_PROJECT}_redis-data|true}" ;;
    *) exit 3 ;;
  esac
  exit
fi

if [ "$1" = compose ]; then
  case "$*" in
    *'config --quiet') [ "${FAKE_CONFIG_FAILURE:-0}" != 1 ]; exit ;;
    *'ps -q bot') printf 'bot-id\n'; exit ;;
    *'ps -q redis') printf 'redis-id\n'; exit ;;
    *'ps -q tg-api') printf 'tg-api-id\n'; exit ;;
    *'ps -q -a volume-init') printf 'volume-init-id\n'; exit ;;
    *'up -d --no-deps --no-build bot')
      if [ "${FAKE_ACTIVATION_FAILURE:-0}" = 1 ] && [ "$APP_RELEASE" = "$RELEASE_SHA" ] && [ ! -e "$FAKE_STATE_DIR/activation-failed" ]; then
        : > "$FAKE_STATE_DIR/activation-failed"
        exit 17
      fi
      printf '%s' "$APP_RELEASE" > "$FAKE_RUNTIME_RELEASE"
      printf '%s' "$BOT_IMAGE" > "$FAKE_RUNTIME_IMAGE"
      exit
      ;;
    *'exec -T bot python'*getWebhookInfo*) [ "${FAKE_WEBHOOK_FAILURE:-0}" != 1 ]; exit ;;
    *'exec -T bot python'*) [ "${FAKE_OWNERSHIP_FAILURE:-0}" != 1 ]; exit ;;
  esac
fi
exit 4
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
printf 'curl' >> "$FAKE_COMMAND_LOG"
for arg in "$@"; do printf ' %s' "$arg" >> "$FAKE_COMMAND_LOG"; done
printf '\n' >> "$FAKE_COMMAND_LOG"
release=$(cat "$FAKE_RUNTIME_RELEASE")
case "$*" in
  *'/health')
    printf '{"ok":true}\n'
    exit
    ;;
esac
if [ "${FAKE_PREVIOUS_LEGACY_HEALTH:-0}" = 1 ] && [ "$release" != "$RELEASE_SHA" ]; then
  exit 22
fi
if [ "${FAKE_READY_FAILURE:-0}" = 1 ] && [ "$release" = "$RELEASE_SHA" ]; then
  exit 22
fi
printf '{"ready":true,"release":"%s","local_bot_api":{"required":true,"configured":true,"functional_probe":true}}\n' "$release"
""",
    )
    _write_executable(
        fake_bin / "df",
        """#!/bin/sh
set -eu
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
if [ "${FAKE_DISK_SHORTAGE:-0}" = 1 ]; then
  printf 'fake 1000 999 1 99%% /\n'
else
  printf 'fake 1000000 1 999999 1%% /\n'
fi
""",
    )
    _write_executable(
        fake_bin / "flock",
        """#!/bin/sh
set -eu
if mkdir "$FAKE_STATE_DIR/flock-held" 2>/dev/null; then
  if [ "${FAKE_FLOCK_HOLD_SECONDS:-0}" != 0 ]; then
    sleep "$FAKE_FLOCK_HOLD_SECONDS"
  fi
  rmdir "$FAKE_STATE_DIR/flock-held"
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        fake_bin / "mv",
        """#!/bin/sh
set -eu
/usr/bin/mv "$@"
destination=''
for argument in "$@"; do destination=$argument; done
if [ "${FAKE_SIGNAL_AFTER_CONFIG_SWAP:-0}" = 1 ] && [ "$destination" = "$PROJECT_ROOT/docker-compose.yml" ] && [ ! -e "$FAKE_STATE_DIR/config-signal-sent" ]; then
  : > "$FAKE_STATE_DIR/config-signal-sent"
  kill -TERM "$PPID"
  sleep 0.1
fi
if [ "${FAKE_SIGNAL_AT_MANIFEST_COMMIT:-0}" = 1 ] && [ "$destination" = "$PROJECT_ROOT/.deploy/current.manifest" ] && [ ! -e "$FAKE_STATE_DIR/manifest-signal-sent" ]; then
  : > "$FAKE_STATE_DIR/manifest-signal-sent"
  kill -TERM "$PPID"
  sleep 0.1
fi
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{_bash_path(fake_bin)}:{env['PATH']}",
            "PROJECT_ROOT": _bash_path(root),
            "RELEASE_DIR": _bash_path(release_dir),
            "RELEASE_SHA": RELEASE_SHA,
            "EXPECTED_BRANCH_SHA": RELEASE_SHA,
            "BOT_IMAGE": BOT_IMAGE,
            "COMPOSE_PROJECT": "verified-project",
            "PUBLIC_BASE_URL": "https://bot.example",
            "PYTHON_BIN": _bash_path(Path(sys.executable)),
            "CURL_BIN": _bash_path(fake_bin / "curl"),
            "DF_BIN": _bash_path(fake_bin / "df"),
            "MV_BIN": _bash_path(fake_bin / "mv"),
            "DEPLOY_HEALTH_ATTEMPTS": "1",
            "DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
            "DEPLOY_MIN_FREE_BYTES": "4096",
            "FAKE_COMMAND_LOG": _bash_path(command_log),
            "FAKE_RUNTIME_RELEASE": _bash_path(runtime_release),
            "FAKE_RUNTIME_IMAGE": _bash_path(runtime_image),
            "FAKE_STATE_DIR": _bash_path(state_dir),
        }
    )
    return FakeHost(
        root,
        release_dir,
        fake_bin,
        state_dir,
        command_log,
        runtime_release,
        runtime_image,
        old_compose,
        env,
    )


def test_routine_deploy_only_replaces_bot_and_preserves_host_state(
    fake_host: FakeHost,
) -> None:
    secret_before = (fake_host.root / ".env").read_bytes()

    result = fake_host.run()

    assert result.returncode == 0, result.stderr
    commands = fake_host.commands()
    assert f"docker pull {BOT_IMAGE}" in commands
    assert (
        "docker compose -p verified-project up -d --no-deps --no-build bot" in commands
    )
    assert not any(
        " up " in command and not command.endswith(" bot") for command in commands
    )
    assert (fake_host.root / ".env").read_bytes() == secret_before
    assert fake_host.runtime_release.read_text(encoding="utf-8") == RELEASE_SHA
    assert fake_host.runtime_image.read_text(encoding="utf-8") == BOT_IMAGE


def test_readiness_failure_restores_previous_image_and_config(
    fake_host: FakeHost,
) -> None:
    result = fake_host.run(FAKE_READY_FAILURE="1")

    assert result.returncode != 0
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE
    assert (fake_host.root / "docker-compose.yml").read_text() == fake_host.old_compose
    activation = "docker compose -p verified-project up -d --no-deps --no-build bot"
    assert fake_host.commands().count(activation) == 1
    assert any(
        "rollback-bot.override.yml up -d --no-deps --no-build bot" in command
        for command in fake_host.commands()
    )


def test_stale_branch_sha_is_a_successful_noop(fake_host: FakeHost) -> None:
    result = fake_host.run(EXPECTED_BRANCH_SHA="e" * 40)

    assert result.returncode == 0
    assert "stale" in result.stdout.lower()
    assert fake_host.commands() == []
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE


def test_concurrent_deploy_is_rejected_by_project_lock(fake_host: FakeHost) -> None:
    first_env = fake_host.env | {"FAKE_FLOCK_HOLD_SECONDS": "2"}
    script = ROOT / "scripts" / "deploy-release.sh"
    first = subprocess.Popen(
        [str(BASH), _bash_path(script)],
        cwd=fake_host.root,
        env=first_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lock_marker = fake_host.state_dir / "flock-held"
    deadline = time.monotonic() + 3
    while not lock_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert lock_marker.exists(), "first deploy never acquired its project lock"

    second = fake_host.run()
    first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode == 0, first_stderr or first_stdout
    assert second.returncode != 0
    assert "already running" in second.stderr.lower()


def test_disk_shortage_stops_before_pull_or_activation(fake_host: FakeHost) -> None:
    result = fake_host.run(FAKE_DISK_SHORTAGE="1")

    assert result.returncode != 0
    assert "disk" in result.stderr.lower()
    assert not any(
        command.startswith("docker pull ") for command in fake_host.commands()
    )
    assert not any(" up " in command for command in fake_host.commands())


def test_project_root_must_match_existing_compose_identity(fake_host: FakeHost) -> None:
    result = fake_host.run(FAKE_PROJECT_WORKING_DIR="/opt/different-project")

    assert result.returncode != 0
    assert "project root" in result.stderr.lower()
    assert not any(
        command.startswith("docker pull ") for command in fake_host.commands()
    )


def test_routine_deploy_requires_recorded_bootstrap_topology(
    fake_host: FakeHost,
) -> None:
    (fake_host.root / ".deploy" / "bootstrap.manifest").unlink()

    result = fake_host.run()

    assert result.returncode != 0
    assert "bootstrap" in result.stderr.lower()
    assert not any(
        command.startswith("docker pull ") for command in fake_host.commands()
    )


def test_bootstrap_gate_records_only_verified_existing_topology(
    fake_host: FakeHost,
) -> None:
    evidence = fake_host.root / ".deploy" / "bootstrap.manifest"
    evidence.unlink()

    result = fake_host.run("bootstrap-production.sh", BOOTSTRAP_MODE="record")

    assert result.returncode == 0, result.stderr
    assert "MEDIA_VOLUME=verified-project_media" in evidence.read_text()
    assert "STATE_VOLUME=verified-project_state" in evidence.read_text()
    assert "TG_API_VOLUME=verified-project_tg-api-data" in evidence.read_text()
    assert "REDIS_VOLUME=verified-project_redis-data" in evidence.read_text()
    assert not any(" up " in command for command in fake_host.commands())


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("FAKE_BOT_MEDIA_MOUNT", "wrong_media|true"),
        ("FAKE_BOT_STATE_MOUNT", "wrong_state|true"),
        ("FAKE_TG_MEDIA_MOUNT", "verified-project_media|true"),
        ("FAKE_TG_SESSION_MOUNT", "wrong_session|true"),
        ("FAKE_REDIS_MOUNT", "wrong_redis|true"),
        ("FAKE_OWNERSHIP_FAILURE", "1"),
    ),
)
def test_routine_deploy_rejects_unbootstrapped_mount_or_ownership(
    fake_host: FakeHost,
    override: str,
    value: str,
) -> None:
    result = fake_host.run(**{override: value})

    assert result.returncode != 0
    assert "bootstrap" in result.stderr.lower()
    assert not any(
        command.startswith("docker pull ") for command in fake_host.commands()
    )


def test_interrupted_activation_runs_rollback(fake_host: FakeHost) -> None:
    result = fake_host.run(FAKE_ACTIVATION_FAILURE="1")

    assert result.returncode != 0
    activation = "docker compose -p verified-project up -d --no-deps --no-build bot"
    assert fake_host.commands().count(activation) == 1
    assert any(
        "rollback-bot.override.yml up -d --no-deps --no-build bot" in command
        for command in fake_host.commands()
    )
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE
    assert (fake_host.root / "docker-compose.yml").read_text() == fake_host.old_compose


def test_signal_after_active_config_swap_restores_previous_release(
    fake_host: FakeHost,
) -> None:
    result = fake_host.run(FAKE_SIGNAL_AFTER_CONFIG_SWAP="1")

    assert result.returncode != 0
    assert (fake_host.state_dir / "config-signal-sent").exists()
    assert (fake_host.root / "docker-compose.yml").read_text() == fake_host.old_compose
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE


def test_manual_rollback_restores_authoritative_previous_current_manifest(
    fake_host: FakeHost,
) -> None:
    current_manifest = fake_host.root / ".deploy" / "current.manifest"
    previous_manifest = (
        f"RELEASE_SHA={PREVIOUS_RELEASE}\n"
        f"BOT_IMAGE={PREVIOUS_IMAGE}\n"
        "COMPOSE_PROJECT=verified-project\n"
    )
    current_manifest.write_text(previous_manifest, encoding="utf-8")

    deploy = fake_host.run()
    assert deploy.returncode == 0, deploy.stderr
    assert (
        current_manifest.read_bytes()
        == (fake_host.release_dir / "release.manifest").read_bytes()
    )

    rollback = fake_host.run("rollback-release.sh")

    assert rollback.returncode == 0, rollback.stderr
    assert current_manifest.read_text(encoding="utf-8") == previous_manifest
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE


def test_signal_after_candidate_manifest_commit_restores_legacy_absence(
    fake_host: FakeHost,
) -> None:
    current_manifest = fake_host.root / ".deploy" / "current.manifest"
    assert not current_manifest.exists()

    result = fake_host.run(FAKE_SIGNAL_AT_MANIFEST_COMMIT="1")

    assert result.returncode != 0
    assert (fake_host.state_dir / "manifest-signal-sent").exists()
    assert not current_manifest.exists()
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE


def test_rollback_override_pins_previous_image_despite_hardcoded_compose(
    fake_host: FakeHost,
) -> None:
    hardcoded = "services:\n  bot:\n    image: ghcr.io/example/ytdlbot:latest\n"
    (fake_host.root / "docker-compose.yml").write_text(hardcoded)

    result = fake_host.run(FAKE_READY_FAILURE="1")

    assert result.returncode != 0
    assert (fake_host.root / "docker-compose.yml").read_text() == hardcoded
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE
    assert any(
        "rollback-bot.override.yml up -d --no-deps --no-build bot" in command
        for command in fake_host.commands()
    )


def test_rollback_uses_recorded_legacy_health_contract_for_baseline_migration(
    fake_host: FakeHost,
) -> None:
    result = fake_host.run(
        FAKE_PREVIOUS_LEGACY_HEALTH="1",
        FAKE_READY_FAILURE="1",
    )

    assert result.returncode != 0
    assert "rollback failed" not in result.stderr.lower()
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert any(command.endswith("/health") for command in fake_host.commands())


def test_release_directory_rejects_files_outside_allowlist(fake_host: FakeHost) -> None:
    (fake_host.release_dir / "unexpected.env").write_text("SECRET=nope\n")

    result = fake_host.run()

    assert result.returncode != 0
    assert "allowlist" in result.stderr.lower()
    assert not any(
        command.startswith("docker pull ") for command in fake_host.commands()
    )


def test_deploy_rejects_release_path_before_executing_candidate(
    fake_host: FakeHost,
) -> None:
    unsafe_release = fake_host.root / "untrusted"
    unsafe_scripts = unsafe_release / "scripts"
    unsafe_scripts.mkdir(parents=True)
    marker = fake_host.state_dir / "unsafe-preflight-ran"
    _write_executable(
        unsafe_scripts / "preflight-production.sh",
        f"#!/bin/sh\n: > '{_bash_path(marker)}'\n",
    )

    result = fake_host.run(RELEASE_DIR=_bash_path(unsafe_release))

    assert result.returncode != 0
    assert not marker.exists()
    assert fake_host.commands() == []


def test_deploy_lock_state_cannot_escape_verified_project(fake_host: FakeHost) -> None:
    outside_state = fake_host.root.parent / "shared-deploy-state"

    result = fake_host.run(DEPLOY_STATE_DIR=_bash_path(outside_state))

    assert result.returncode != 0
    assert not outside_state.exists()
    assert fake_host.commands() == []


def test_manifest_must_match_exact_sha_image_project_and_compose(
    fake_host: FakeHost,
) -> None:
    manifest = fake_host.release_dir / "release.manifest"
    manifest.write_text(
        manifest.read_text().replace(
            f"RELEASE_SHA={RELEASE_SHA}", f"RELEASE_SHA={'f' * 40}"
        )
    )

    result = fake_host.run()

    assert result.returncode != 0
    assert "manifest" in result.stderr.lower()
    assert fake_host.commands() == []


def test_webhook_verification_failure_rolls_back(fake_host: FakeHost) -> None:
    result = fake_host.run(FAKE_WEBHOOK_FAILURE="1")

    assert result.returncode != 0
    activation = "docker compose -p verified-project up -d --no-deps --no-build bot"
    assert fake_host.commands().count(activation) == 1
    assert any(
        "rollback-bot.override.yml up -d --no-deps --no-build bot" in command
        for command in fake_host.commands()
    )
    assert fake_host.runtime_release.read_text(encoding="utf-8") == PREVIOUS_RELEASE
    assert fake_host.runtime_image.read_text(encoding="utf-8") == PREVIOUS_IMAGE


def test_workflows_gate_exact_sha_build_once_and_validate_known_host() -> None:
    test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    deploy_workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    assert "vps" in test_workflow
    assert 'python-version: ["3.12"]' in test_workflow
    assert "docker compose config" in test_workflow
    assert "bash -n scripts/bootstrap-production.sh" in test_workflow
    assert "scripts/deploy-release.sh" in test_workflow
    assert "scripts/preflight-production.sh" in test_workflow
    assert "scripts/rollback-release.sh" in test_workflow

    assert "needs: verify" in deploy_workflow
    assert "cancel-in-progress: false" in deploy_workflow
    assert "tags: ${{ steps.image.outputs.image }}:${{ github.sha }}" in deploy_workflow
    assert "digest: ${{ steps.build.outputs.digest }}" in deploy_workflow
    assert "git/ref/heads/vps" in deploy_workflow
    assert "fingerprint: ${{ secrets.VPS_HOST_FINGERPRINT }}" in deploy_workflow
    assert "release-payload/docker-compose.yml" in deploy_workflow
    assert "release-payload/release.manifest" in deploy_workflow
    assert "release-payload/scripts/deploy-release.sh" in deploy_workflow
    assert 'source: "release-payload/*"' not in deploy_workflow


def test_clean_ci_installs_all_pinned_test_dependencies() -> None:
    ci_requirements = (ROOT / "requirements-ci.txt").read_text().splitlines()
    expected = {
        "pytest==9.1.1",
        "pytest-cov==7.1.0",
        "pytest-asyncio==1.4.0",
        "hypothesis==6.168.0",
        "PyYAML==6.0.3",
        "httpx==0.28.1",
    }
    assert expected <= set(ci_requirements)
    for workflow_name in ("test.yml", "deploy.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text()
        assert "pip install -r requirements-ci.txt" in workflow
        assert "pip install pytest pytest-cov" not in workflow


def test_workflow_smokes_immutable_image_and_promotes_trusted_staging() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()

    assert "Smoke immutable built image" in workflow
    assert 'docker pull "$SMOKE_IMAGE"' in workflow
    assert 'docker run --rm --entrypoint python "$SMOKE_IMAGE"' in workflow
    assert "health_live" in workflow
    assert "Prepare trusted fresh staging directory" in workflow
    assert "readlink -f" in workflow
    assert 'test ! -L "$staging_dir"' in workflow
    assert "/.incoming/" in workflow
    assert 'mv "$staging_dir" "$release_dir"' in workflow


@pytest.mark.parametrize("initial_failure", ["interrupted-promotion", "activation"])
def test_same_sha_promotion_reuses_only_an_identical_immutable_release(
    tmp_path: Path,
    initial_failure: str,
) -> None:
    project_root = tmp_path / "opt" / "ytdlbot"
    incoming = project_root / ".incoming"
    release_dir = project_root / "releases" / RELEASE_SHA
    activation_count = tmp_path / "activation-count"
    incoming.mkdir(parents=True)
    release_dir.parent.mkdir()
    script = _activation_workflow_script()
    base_env = os.environ | {
        "PROJECT_ROOT": _bash_path(project_root),
        "RELEASE_SHA": RELEASE_SHA,
        "EXPECTED_BRANCH_SHA": RELEASE_SHA,
        "BOT_IMAGE": BOT_IMAGE,
        "COMPOSE_PROJECT": "verified-project",
        "PUBLIC_BASE_URL": "https://bot.example",
    }

    def run(upload_id: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(BASH), "-c", script],
            env=base_env | {"DEPLOY_UPLOAD_ID": upload_id} | overrides,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    first_staging = incoming / "upload-first"
    _write_uploaded_release(first_staging, activation_count)
    if initial_failure == "interrupted-promotion":
        first_staging.replace(release_dir)
        expected_activations = "1"
    else:
        first = run("upload-first", FAKE_FAIL_FIRST_ACTIVATION="1")
        assert first.returncode == 42, (first.stdout, first.stderr)
        assert activation_count.read_text(encoding="utf-8") == "1"
        expected_activations = "2"

    release_hashes = {
        path.relative_to(release_dir): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release_dir.rglob("*")
        if path.is_file()
    }
    mismatched_staging = incoming / "upload-mismatch"
    _write_uploaded_release(mismatched_staging, activation_count)
    with (mismatched_staging / "scripts" / "rollback-release.sh").open("a") as file:
        file.write("# changed payload\n")

    mismatch = run("upload-mismatch")

    assert mismatch.returncode != 0
    assert {
        path.relative_to(release_dir): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release_dir.rglob("*")
        if path.is_file()
    } == release_hashes

    retry_staging = incoming / "upload-retry"
    _write_uploaded_release(retry_staging, activation_count)
    retry = run("upload-retry")

    assert retry.returncode == 0, retry.stderr
    assert activation_count.read_text(encoding="utf-8") == expected_activations
    assert {
        path.relative_to(release_dir): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release_dir.rglob("*")
        if path.is_file()
    } == release_hashes


def test_all_first_party_actions_are_pinned_to_full_commit_sha() -> None:
    workflows = "\n".join(
        (ROOT / ".github" / "workflows" / name).read_text()
        for name in ("test.yml", "deploy.yml")
    )
    first_party = ("actions/", "docker/")
    uses_lines = [line.strip() for line in workflows.splitlines() if "uses:" in line]
    for line in uses_lines:
        action = line.split("uses:", 1)[1].strip()
        if action.startswith(first_party):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}(?:\s+#.*)?", action), action


def test_release_automation_contains_no_host_wide_or_secret_rewrite_operations() -> (
    None
):
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "scripts" / "deploy-release.sh",
            ROOT / "scripts" / "preflight-production.sh",
            ROOT / "scripts" / "rollback-release.sh",
            ROOT / ".github" / "workflows" / "deploy.yml",
        )
        if path.exists()
    ).lower()

    forbidden = (
        "docker compose down",
        "--remove-orphans",
        "docker compose pull",
        "docker image prune",
        "systemctl restart docker",
        "service docker restart",
        "caddy reload",
        "portainer",
        "reboot",
        "cat > .env",
        "tee .env",
    )
    assert all(command not in sources for command in forbidden)
