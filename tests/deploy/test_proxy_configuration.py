from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from tests.deploy.test_release_scripts import ROOT, FakeHost

pytest_plugins = ["tests.deploy.test_release_scripts"]

PROXY = "socks5://trial:p$a'ss%22@proxy.example:1080"


def encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


@pytest.mark.parametrize("value", [PROXY, json.dumps([PROXY]), "[]"])
def test_release_applies_only_explicit_proxy_configuration(fake_host: FakeHost, value):
    environment = fake_host.root / ".env"
    prefix = "# protected settings\nBOT_TOKEN=keep-secret\n"
    suffix = "# retained comment\nVK_PROXY=keep-existing\n"
    environment.write_text(prefix + "MEDIA_PROXY_URLS=[]\n" + suffix, encoding="utf-8")
    before = environment.read_bytes()

    result = fake_host.run(MEDIA_PROXY_URLS_B64=encoded(value))

    assert result.returncode == 0, result.stderr
    expected = [] if value == "[]" else [PROXY]
    assert json.loads(dotenv_values(environment)["MEDIA_PROXY_URLS"]) == expected
    updated = environment.read_text(encoding="utf-8")
    assert updated.startswith(prefix) and updated.endswith(suffix)
    assert (fake_host.root / ".deploy/rollback.env").read_bytes() == before
    observed = result.stdout + result.stderr + "\n".join(fake_host.commands())
    assert PROXY not in observed and encoded(value) not in observed


@pytest.mark.parametrize(
    "value",
    [
        "[broken",
        "http://trial:password@proxy.example:1080",
        "socks5://host",
        "socks5://host:1080\nBAD=value",
    ],
)
def test_invalid_proxy_secret_leaves_running_configuration_untouched(fake_host, value):
    before = (fake_host.root / ".env").read_bytes()

    result = fake_host.run(MEDIA_PROXY_URLS_B64=encoded(value))

    assert result.returncode != 0
    assert (fake_host.root / ".env").read_bytes() == before
    assert not any(" up " in command for command in fake_host.commands())
    assert value not in result.stdout + result.stderr


def test_failed_activation_restores_proxy_configuration(fake_host: FakeHost):
    before = (fake_host.root / ".env").read_bytes()

    result = fake_host.run(MEDIA_PROXY_URLS_B64=encoded(PROXY), FAKE_READY_FAILURE="1")

    assert result.returncode != 0
    assert (fake_host.root / ".env").read_bytes() == before
    assert PROXY not in result.stdout + result.stderr


def test_manual_rollback_restores_proxy_configuration(fake_host: FakeHost):
    before = (fake_host.root / ".env").read_bytes()
    result = fake_host.run(MEDIA_PROXY_URLS_B64=encoded(PROXY))
    assert result.returncode == 0, result.stderr

    rollback = fake_host.run("rollback-release.sh")

    assert rollback.returncode == 0, rollback.stderr
    assert (fake_host.root / ".env").read_bytes() == before


def test_workflow_encodes_secret_before_ssh_forwarding(tmp_path: Path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    encode = next(
        step
        for step in steps
        if step.get("name") == "Encode optional media proxy secret"
    )
    assert encode["env"]["MEDIA_PROXY_URLS"] == "${{ secrets.MEDIA_PROXY_URLS }}"
    code = encode["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    output = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=os.environ | {"MEDIA_PROXY_URLS": PROXY, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert PROXY not in result.stdout + result.stderr
    assert output.read_text().strip() == f"encoded={encoded(PROXY)}"
    assert f"::add-mask::{encoded(PROXY)}" in result.stdout
    activate = next(
        step
        for step in steps
        if step.get("name") == "Activate immutable release over verified SSH"
    )
    assert (
        activate["env"]["MEDIA_PROXY_URLS_B64"]
        == "${{ steps.proxy_secret.outputs.encoded }}"
    )
    assert "MEDIA_PROXY_URLS_B64" in activate["with"]["envs"].split(",")
