from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASH = next(
    (
        candidate
        for candidate in (
            Path(r"C:\Program Files\Git\bin\bash.exe"),
            Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        )
        if candidate.exists()
    ),
    Path(shutil.which("bash") or "bash"),
)


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if re.fullmatch(r"[A-Za-z]:", resolved.drive):
        return f"/{resolved.drive[0].lower()}{resolved.as_posix().split(':', 1)[1]}"
    return resolved.as_posix()


def _write_command(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.mark.parametrize("tunnel_status", [0, 17])
def test_wireguard_example_starts_the_prebuilt_proxy(
    tmp_path: Path, tunnel_status: int
) -> None:
    compose = yaml.safe_load(
        (ROOT / "docker-compose.wireguard.example.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["wireguard-proxy"]
    build_context = ROOT / service["build"]["context"]
    dockerfile = (build_context / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = next(
        json.loads(line.partition(" ")[2])
        for line in dockerfile.splitlines()
        if line.startswith("ENTRYPOINT ")
    )
    assert "entrypoint" not in service and "command" not in service
    script = next(
        build_context / source
        for line in dockerfile.splitlines()
        if line.startswith("COPY ")
        for source, destination in [line.split()[-2:]]
        if destination == entrypoint[0]
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    for name, exit_status in (("wg-quick", tunnel_status), ("microsocks", 23)):
        _write_command(
            commands / name,
            '#!/bin/sh\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$TRACE"\n'
            f"exit {exit_status}\n",
        )
    for name in ("apk", "apt-get", "curl", "gcc", "git", "make", "wget"):
        _write_command(
            commands / name,
            '#!/bin/sh\nprintf "%s\\n" "${0##*/}" >> "$TRACE"\nexit 97\n',
        )

    trace = tmp_path / "startup.log"
    result = subprocess.run(
        [
            str(BASH),
            "--noprofile",
            "--norc",
            "-c",
            'export PATH="$1"; exec /bin/sh "$2"',
            "wireguard-example",
            _bash_path(commands),
            _bash_path(script),
        ],
        env={**os.environ, "TRACE": _bash_path(trace)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    observed = trace.read_text(encoding="utf-8").splitlines()
    if tunnel_status:
        assert result.returncode == 1
        assert "WireGuard failed to start" in result.stderr
        assert observed == ["wg-quick up /config/wg0.conf"]
    else:
        assert result.returncode == 23
        assert observed == ["wg-quick up /config/wg0.conf", "microsocks -p 1080"]


@pytest.mark.parametrize("compose_status", [0, 17])
def test_ci_compose_validation_uses_and_removes_its_environment_fixture(
    tmp_path: Path, compose_status: int
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    )
    step = next(
        item
        for item in workflow["jobs"]["test"]["steps"]
        if item.get("name") == "Validate immutable release contracts"
    )
    for name in (".env.example", "docker-compose.yml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for source in (ROOT / "scripts").glob("*.sh"):
        (scripts / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    commands = tmp_path / "commands"
    commands.mkdir()
    _write_command(
        commands / "docker",
        "#!/bin/sh\nset -eu\n"
        "test \"$*\" = 'compose config --quiet'\n"
        "test -f .env || { echo 'Compose env_file is missing' >&2; exit 23; }\n"
        "cmp .env.example .env\n"
        "printf 'validated\\n' > compose-result\n"
        f"exit {compose_status}\n",
    )
    _write_command(commands / "python", "#!/bin/sh\nexit 0\n")
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in step.get("env", {}).items()})
    command = f"export PATH='{_bash_path(commands)}':$PATH\n{step['run']}"
    result = subprocess.run(
        [str(BASH), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == compose_status, result.stdout + result.stderr
    assert (tmp_path / "compose-result").read_text() == "validated\n"
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("workflow_name", ["test.yml", "deploy.yml"])
@pytest.mark.parametrize(
    "broken_script",
    [
        None,
        "bootstrap-production.sh",
        "bootstrap-migrate-production.sh",
        "deploy-release.sh",
        "preflight-production.sh",
        "rollback-release.sh",
    ],
)
def test_ci_syntax_gate_checks_each_release_script(
    tmp_path: Path, workflow_name: str, broken_script: str | None
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
    )
    step = next(
        item
        for job in workflow["jobs"].values()
        for item in job["steps"]
        if "bash -n" in item.get("run", "")
    )
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for source in (ROOT / "scripts").glob("*.sh"):
        (scripts / source.name).write_text(
            "if then\n" if source.name == broken_script else "exit 37\n",
            encoding="utf-8",
            newline="\n",
        )
    commands = tmp_path / "commands"
    commands.mkdir()
    _write_command(commands / "docker", "#!/bin/sh\n: > compose-result\n")
    _write_command(commands / "python", "#!/bin/sh\nexit 0\n")
    command = f"export PATH='{_bash_path(commands)}':$PATH\n{step['run']}"
    result = subprocess.run(
        [str(BASH), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if broken_script is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "compose-result").exists()
    else:
        assert result.returncode != 0, f"CI accepted invalid {broken_script}"
        assert not (tmp_path / "compose-result").exists()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("workflow_name", ["test.yml", "deploy.yml", "integration.yml"])
def test_ci_test_steps_receive_the_webhook_fixture_secret(
    workflow_name: str,
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    job_name = {
        "test.yml": "test",
        "deploy.yml": "verify",
        "integration.yml": "integration",
    }[workflow_name]
    job = jobs[job_name]
    step = next(
        item for item in job["steps"] if "python -m pytest" in item.get("run", "")
    )
    env = os.environ.copy()
    for settings in (workflow.get("env", {}), job.get("env", {}), step.get("env", {})):
        env.update({str(key): str(value) for key, value in settings.items()})
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "tests/test_webhook_durability.py::test_webhook_returns_success_only_after_transactional_insert",
            "-o",
            "addopts=",
            "--no-cov",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
