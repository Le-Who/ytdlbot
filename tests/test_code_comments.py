from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-code-comments.py"
BASELINE = "scripts/code-comments-baseline.json"


def write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8", newline="\n")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
    ).strip()


def commit(root: Path) -> str:
    git(root, "add", ".")
    git(root, "commit", "--quiet", "--no-gpg-sign", "-m", "Test fixture")
    return git(root, "rev-parse", "HEAD")


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *args],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    return tmp_path


def baseline(root: Path, entries: dict[str, dict[str, int]]) -> None:
    write(root, BASELINE, json.dumps(entries))


def test_inventory_detects_repository_comment_syntax_and_executable_literals(
    repository: Path,
) -> None:
    examples = {
        "app/example.py": (
            "#!/usr/bin/env python3\n"
            '"""# is docstring content."""\n'
            'value = "# is string content"  # cleanup needed\n'
            "value = 1  # noqa: F401\n"
            "value = 2  # noqa: F401 - explain the assignment\n"
            "#!/usr/bin/env python3\n"
        ),
        "app/typing_case.py": "# mypy: ignore-errors\n",
        "scripts/example.sh": (
            "#!/bin/sh\n"
            "echo a#literal '# literal' \\#literal ${a##x} # bare comment\n"
            "cat <<'TEXT'\n# literal\nTEXT\n"
            "python3 - <<'PY'\nvalue = '# literal' # Python comment\nPY\n"
            "# shellcheck disable=SC2086\n"
            "# final comment"
        ),
        "scripts/python-literal.sh": (
            "python3 - <<'PY'\n"
            "# shellcheck disable=SC2086\n"
            "value = some_module  # type: ignore[name-defined]\n"
            "PY\n"
        ),
        "Open Logs.ps1": (
            "#Requires -Version 7\n"
            'Write-Output foo#literal "# literal" `#literal # PowerShell comment\n'
            '$value = @"\n# literal\n"@\n'
            "<# block\nPowerShell comment #>\n"
        ),
        "Open Logs.vbs": "x = \"it's quoted\" : REM comment\n'apostrophe comment\n",
        ".github/workflows/example.yml": (
            "name: Example # YAML comment\n"
            "jobs:\n"
            "  run:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            '      - run: |\n          echo "# literal"\n          # GitHub shell comment\n'
            "      - uses: appleboy/ssh-action@pinned\n"
            "        with:\n"
            "          script: |\n            # SSH comment\n            true\n"
        ),
        "compose.example.yaml": (
            "services:\n"
            "  proxy:\n"
            "    entrypoint: /bin/bash\n"
            "    command: >\n"
            '      -c "\n'
            "        # Compose shell comment\n"
            "        echo '# literal'\n"
            '      "\n'
        ),
        "config.yaml": (
            'text: "foo # literal"\nother: foo#bar\n'
            "payload: |\n  # literal\n"
            "other_payload: | # YAML comment\n  # literal\n"
        ),
        "Dockerfile": (
            "# syntax=docker/dockerfile:1.7\n"
            "FROM base\n"
            "# escape=`\n"
            "ENV PATTERN=foo#literal\n"
            "RUN echo '# literal' # Docker shell comment\n"
            "COPY <<EOF /file\n# literal payload\nEOF\n"
            "RUN <<EOF\n# Docker heredoc comment\necho '# string'\nEOF\n"
            "RUN [ -f /file ] # Docker bracket command comment\n"
            "CMD echo \\\n  literal # Docker continuation comment\n"
            "ENTRYPOINT echo literal # Docker entrypoint comment\n"
            'ENTRYPOINT ["sh", "-c", "echo literal # Docker exec comment"]\n'
            'CMD ["echo", "# literal"]\n'
        ),
        "pyproject.toml": 'value = """\n# literal\n""" # TOML comment\n',
        ".env.example": (
            "LITERAL=#is-part-of-value\n"
            "VALUE='multi-line\n# literal' # dotenv comment\n"
            "UNQUOTED=foo#bar # unquoted comment\n"
        ),
        ".gitignore": "# ignore comment\n\\#literal\nfile#literal\n",
        "requirements-ci.txt": "tool @ https://example.org/tool.whl#tag # reference\n",
        "fixture.html": '<!DOCTYPE html><div id="a#b"><!-- HTML comment --></div>\n',
        "scripts/media-acceptance-docker-adapter.py": (
            'other_literal = """\n# literal\n"""\n'
            '_CONTAINER_HELPER = r"""\n# embedded Python comment\n"""\n'
            '_LEGACY_RUN_SHELL = r"""\n# embedded shell comment\n"""\n'
        ),
        "README.md": "# Heading\n<!-- prose documentation -->\n",
        "fixture.json": '{"string": "# literal"}\n',
        "untracked-ignored.py": "# ignored source\n",
    }
    for path, source in examples.items():
        write(repository, path, source)
    write(
        repository,
        ".gitignore",
        examples[".gitignore"] + "untracked-ignored.py\n",
    )
    result = run(repository, "--inventory")
    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert {
        (item["path"], item["line"], item["text"])
        for item in inventory
        if not item["directive"]
    } == {
        ("app/example.py", 3, "# cleanup needed"),
        ("app/example.py", 5, "# noqa: F401 - explain the assignment"),
        ("app/example.py", 6, "#!/usr/bin/env python3"),
        ("scripts/example.sh", 2, "# bare comment"),
        ("scripts/example.sh", 7, "# Python comment"),
        ("scripts/example.sh", 10, "# final comment"),
        ("scripts/python-literal.sh", 2, "# shellcheck disable=SC2086"),
        ("Open Logs.ps1", 2, "# PowerShell comment"),
        ("Open Logs.ps1", 6, "<# block\nPowerShell comment #>"),
        ("Open Logs.vbs", 1, "REM comment"),
        ("Open Logs.vbs", 2, "'apostrophe comment"),
        (".github/workflows/example.yml", 1, "# YAML comment"),
        (".github/workflows/example.yml", 8, "# GitHub shell comment"),
        (".github/workflows/example.yml", 12, "# SSH comment"),
        ("compose.example.yaml", 6, "# Compose shell comment"),
        ("config.yaml", 5, "# YAML comment"),
        ("Dockerfile", 3, "# escape=`"),
        ("Dockerfile", 5, "# Docker shell comment"),
        ("Dockerfile", 10, "# Docker heredoc comment"),
        ("Dockerfile", 13, "# Docker bracket command comment"),
        ("Dockerfile", 15, "# Docker continuation comment"),
        ("Dockerfile", 16, "# Docker entrypoint comment"),
        ("Dockerfile", 17, "# Docker exec comment"),
        ("pyproject.toml", 3, "# TOML comment"),
        (".env.example", 3, "# dotenv comment"),
        (".env.example", 4, "# unquoted comment"),
        (".gitignore", 1, "# ignore comment"),
        ("requirements-ci.txt", 1, "# reference"),
        ("fixture.html", 1, "<!-- HTML comment -->"),
        ("scripts/media-acceptance-docker-adapter.py", 5, "# embedded Python comment"),
        ("scripts/media-acceptance-docker-adapter.py", 8, "# embedded shell comment"),
    }
    assert {item["text"] for item in inventory if item["directive"]} == {
        "#!/usr/bin/env python3",
        "#!/bin/sh",
        "# noqa: F401",
        "# mypy: ignore-errors",
        "# type: ignore[name-defined]",
        "#Requires -Version 7",
        "# shellcheck disable=SC2086",
        "# syntax=docker/dockerfile:1.7",
    }


def test_new_changed_and_duplicate_comments_fail_without_allowance(
    repository: Path,
) -> None:
    write(repository, "example.py", "# existing\nvalue = 1\n")
    baseline(repository, {"example.py": {"# existing": 1}})
    commit(repository)

    write(repository, "example.py", "\nvalue = 2\n# existing\n")
    assert run(repository).returncode == 0
    write(repository, "example.py", "# existing\n# existing\n# reworded\n")
    write(repository, "new.py", "# existing\n")
    result = run(repository)
    assert result.returncode == 1
    assert "example.py:2: new prose comment: '# existing'" in result.stderr
    assert "example.py:3: new prose comment: '# reworded'" in result.stderr
    assert "new.py:1: new prose comment: '# existing'" in result.stderr
    assert run(repository, "--prune").returncode == 1


def test_pruning_removes_deleted_occurrences_and_never_replenishes_them(
    repository: Path,
) -> None:
    write(repository, "example.py", "# existing\n# existing\n# gone\n")
    baseline(repository, {"example.py": {"# existing": 2, "# gone": 1}})
    commit(repository)
    write(repository, "example.py", "# existing\n")

    stale = run(repository)
    assert stale.returncode == 1
    assert "unused baseline allowance" in stale.stderr
    assert run(repository, "--prune").returncode == 0
    assert json.loads((repository / BASELINE).read_text(encoding="utf-8")) == {
        "example.py": {"# existing": 1},
    }
    previous = commit(repository)

    write(repository, "example.py", "# existing\n# existing\n")
    baseline(repository, {"example.py": {"# existing": 2}})
    commit(repository)
    result = run(repository, "--base", previous)
    assert result.returncode == 1
    assert "baseline grew (1): '# existing'" in result.stderr


def test_first_baseline_cannot_add_new_comments(repository: Path) -> None:
    write(repository, "example.py", "# before the rule\n")
    previous = commit(repository)
    baseline(repository, {"example.py": {"# before the rule": 1}})
    assert run(repository, "--base", previous).returncode == 0

    write(repository, "example.py", "# before the rule\n# added with the rule\n")
    baseline(
        repository, {"example.py": {"# before the rule": 1, "# added with the rule": 1}}
    )
    result = run(repository, "--base", previous)
    assert result.returncode == 1
    assert "baseline grew (1): '# added with the rule'" in result.stderr
