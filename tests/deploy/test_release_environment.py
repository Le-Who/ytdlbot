from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from tests.deploy import test_release_scripts as release_scripts
from tests.deploy.test_release_scripts import (
    BASH,
    FakeHost,
    _bash_path,
    _run_fixture_bash,
)

pytest_plugins = ["tests.deploy.test_release_scripts"]


@pytest.mark.parametrize(
    "entrypoint",
    [
        "deploy-release.sh",
        "bootstrap-production.sh",
        "bootstrap-migrate-production.sh",
        "rollback-release.sh",
    ],
)
def test_release_entrypoints_do_not_inherit_compose_file_or_environment_overrides(
    fake_host: FakeHost, entrypoint: str
) -> None:
    if entrypoint == "rollback-release.sh":
        activation = fake_host.run()
        assert activation.returncode == 0, activation.stderr
    if entrypoint == "bootstrap-migrate-production.sh":
        (fake_host.root / ".deploy/bootstrap.manifest").unlink()

    docker = fake_host.fake_bin / "docker"
    original = docker.read_text(encoding="utf-8")
    # Model Compose's selection boundary: -f overrides COMPOSE_FILE; otherwise
    # an inherited COMPOSE_FILE replaces the canonical project configuration.
    probe = """if [ "$1" = compose ]; then
  effective_file=${COMPOSE_FILE:-$PROJECT_ROOT/docker-compose.yml}
  previous=''
  for argument in "$@"; do
    if [ "$previous" = -f ]; then effective_file=$argument; break; fi
    previous=$argument
  done
  printf '%s\n' "$effective_file" >> "$FAKE_STATE_DIR/effective-compose-files"
  if [ "${COMPOSE_ENV_FILES+x}${COMPOSE_DISABLE_ENV_FILE+x}${COMPOSE_PROFILES+x}${COMPOSE_PROJECT_NAME+x}" != '' ]; then
    printf 'inherited\n' >> "$FAKE_STATE_DIR/inherited-compose-environment"
  fi
fi
"""
    docker.write_text(
        original.replace("set -eu\n", "set -eu\n" + probe, 1),
        encoding="utf-8",
        newline="\n",
    )
    result = fake_host.run(
        entrypoint,
        COMPOSE_FILE="/outside-project/other-compose.yml",
        COMPOSE_PROJECT_NAME="neighbor-project",
        COMPOSE_PROFILES="unreviewed-profile",
        COMPOSE_ENV_FILES="/outside-project/other.env",
        COMPOSE_DISABLE_ENV_FILE="1",
    )

    assert result.returncode == 0, result.stderr
    observed = (
        (fake_host.state_dir / "effective-compose-files").read_text().splitlines()
    )
    assert observed
    assert "/outside-project/other-compose.yml" not in observed
    assert all(Path(path).name != "other-compose.yml" for path in observed)
    assert not (fake_host.state_dir / "inherited-compose-environment").exists()


def test_simulated_release_watchdog_stops_its_child_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "late-child-write"
    release = tmp_path / "release-child"
    child = (
        "printf 'ready\\n'; "
        f"while [ ! -f '{_bash_path(release)}' ]; do sleep 0.1; done; "
        f"printf leaked > '{_bash_path(marker)}'"
    )
    command = [str(BASH), "-c", f"sh -c {shlex.quote(child)} & wait"]
    original_start = release_scripts._start_fixture_bash

    def start_after_child_is_running(*args, **kwargs):
        process, job = original_start(*args, **kwargs)
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        return process, job

    monkeypatch.setattr(
        release_scripts, "_start_fixture_bash", start_after_child_is_running
    )

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _run_fixture_bash(command, env=os.environ.copy(), timeout=0.1)
    finally:
        release.touch()
    time.sleep(1.2)
    assert not marker.exists()
