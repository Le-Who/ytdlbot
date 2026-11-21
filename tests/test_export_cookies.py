"""Exporter contracts use only synthetic cookies and fake external processes."""

from __future__ import annotations

import base64
import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
COOKIE = ".youtube.com\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-only\n"


def _load_exporter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "synthetic_cookie_exporter", ROOT / "tools/export_cookies.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _load_exporter()
    external_attempts = []

    def forbidden_process(*args: Any, **kwargs: Any) -> Any:
        external_attempts.append(args)
        raise AssertionError(
            "external subprocess must be explicitly faked in exporter tests"
        )

    monkeypatch.setattr(module.subprocess, "run", forbidden_process)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden_process)
    yield module
    assert external_attempts == [], (
        "an exporter test reached an unfaked external process"
    )


def test_import_leaves_redirected_standard_streams_untouched(monkeypatch) -> None:
    stdin = Mock(encoding="ascii")
    stdout = Mock(encoding="utf-8")
    stderr = Mock(encoding="ascii")
    with monkeypatch.context() as redirect:
        redirect.setattr(sys, "stdin", stdin)
        redirect.setattr(sys, "stdout", stdout)
        redirect.setattr(sys, "stderr", stderr)
        _load_exporter()
        assert sys.stdin is stdin
        assert sys.stdout is stdout
        assert sys.stderr is stderr

    stdin.reconfigure.assert_not_called()
    stdout.reconfigure.assert_not_called()
    stderr.reconfigure.assert_not_called()


@pytest.fixture
def file_exporter(exporter: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the input-file mode reached the browser or provider path")

    for name in (
        "_check_ytdlp",
        "_pick_browser_and_profile",
        "_check_browser_running",
        "_resolve_profile",
        "_discover_chromium_profiles",
        "_extract_cookies",
        "_copy_to_clipboard",
    ):
        monkeypatch.setattr(exporter, name, forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    return exporter


def test_input_cookie_file_filters_and_emits_complete_environment_line(
    file_exporter, tmp_path, monkeypatch, capsys
) -> None:
    source = tmp_path / "user supplied cookies.txt"
    accepted = "#HttpOnly_" + COOKIE
    unrelated = COOKIE.replace(".youtube.com", ".unrelated.invalid")
    source.write_text(unrelated + accepted, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", "--input", str(source)])

    file_exporter.main()

    captured = capsys.readouterr()
    encoded = base64.b64encode(("# Netscape HTTP Cookie File\n" + accepted).encode())
    assert f"YTDLP_COOKIES_B64={encoded.decode()}" in captured.out
    assert captured.err == ""
    assert source.read_text(encoding="utf-8") == unrelated + accepted


def test_cli_configures_redirected_output_without_replacing_streams(
    file_exporter, tmp_path, monkeypatch
) -> None:
    source = tmp_path / "cookies.txt"
    source.write_text(COOKIE, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", f"--input={source}"])

    with (
        io.TextIOWrapper(io.BytesIO(), encoding="ascii") as stdin,
        io.TextIOWrapper(io.BytesIO(), encoding="ascii") as stdout,
        io.TextIOWrapper(io.BytesIO(), encoding="ascii") as stderr,
        monkeypatch.context() as redirect,
    ):
        redirect.setattr(sys, "stdin", stdin)
        redirect.setattr(sys, "stdout", stdout)
        redirect.setattr(sys, "stderr", stderr)
        file_exporter.main()

        assert sys.stdin is stdin and stdin.encoding == "ascii"
        assert sys.stdout is stdout and stdout.encoding == "utf-8"
        assert sys.stderr is stderr and stderr.encoding == "utf-8"
        assert stdout.line_buffering and stderr.line_buffering
        assert b"YTDLP_COOKIES_B64=" in stdout.buffer.getvalue()


@pytest.mark.parametrize(
    "failure",
    [
        "missing-path",
        "missing-file",
        "invalid-encoding",
        "malformed",
        "browser",
        "duplicate",
    ],
)
def test_invalid_cookie_file_inputs_exit_without_export(
    file_exporter, tmp_path, monkeypatch, capsys, failure
) -> None:
    source = tmp_path / "cookies.txt"
    arguments = ["--input", str(source)]
    if failure == "missing-path":
        arguments = ["--input"]
    elif failure == "invalid-encoding":
        source.write_bytes(b"\xff")
    elif failure == "malformed":
        source.write_text("invalid cookie row", encoding="utf-8")
    elif failure == "browser":
        arguments.insert(0, "firefox")
    elif failure == "duplicate":
        arguments += ["--input", str(tmp_path / "second.txt")]
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", *arguments])

    with pytest.raises(SystemExit) as error:
        file_exporter.main()

    assert error.value.code == (1 if failure == "malformed" else 2)
    output = capsys.readouterr()
    assert "YTDLP_COOKIES_B64=" not in output.out
    assert output.err or "No YouTube/Google cookies found" in output.out


@pytest.mark.parametrize(
    "domain,keep",
    [
        (".youtube.com", True),
        ("youtube.com", True),
        ("m.youtube.com", True),
        (".google.com", True),
        ("accounts.google.com", True),
        (".googlevideo.com", True),
        (".YouTube.COM", True),
        ("#HttpOnly_.youtube.com", True),
        ("youtube.com.evil.invalid", False),
        ("evilyoutube.com", False),
        ("notgoogle.com", False),
        ("example.invalid", False),
        ("", False),
    ],
)
def test_filter_keeps_only_exact_google_domain_boundaries(
    exporter: Any, domain: str, keep: bool
) -> None:
    line = f"{domain}\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-only"
    filtered = exporter._filter_youtube_cookies(
        "# Netscape HTTP Cookie File\n" + line + "\n"
    )
    assert (line in filtered) is keep


@pytest.mark.parametrize(
    "line",
    [
        "youtube.com\tTRUE\t/\tTRUE\t0\tname",
        "youtube.com\tTRUE\t/\tTRUE\t0\tname\tvalue\textra",
        "youtube.com\tTRUE\t/\tTRUE\t0\t\tvalue",
        "youtube.com\tTRUE\t\tTRUE\t0\tname\tvalue",
        "youtube.com\tTRUE\t/\tTRUE\tinvalid\tname\tvalue",
    ],
)
def test_filter_drops_invalid_netscape_rows(exporter: Any, line: str) -> None:
    assert exporter._filter_youtube_cookies(line) == ""


def test_mixed_cookie_file_drops_unrelated_sessions_but_preserves_http_only(
    exporter: Any,
) -> None:
    allowed = "#HttpOnly_.YouTube.COM\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-only\n"
    unrelated = ".unrelated.invalid\tTRUE\t/\tTRUE\t0\tprivate\tfixture-only\n"
    assert (
        exporter._filter_youtube_cookies(unrelated + allowed)
        == "# Netscape HTTP Cookie File\n" + allowed
    )


def test_no_matching_cookie_cannot_reach_clipboard(
    exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "_check_ytdlp", lambda: ["synthetic-yt-dlp"])
    monkeypatch.setattr(exporter, "_check_browser_running", lambda _: False)
    monkeypatch.setattr(exporter, "_resolve_profile", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", "firefox"])
    monkeypatch.setattr(
        exporter,
        "_extract_cookies",
        lambda *args: ".unrelated.invalid\tTRUE\t/\tTRUE\t0\tsession\tsynthetic-only\n",
    )
    copied = []
    monkeypatch.setattr(
        exporter, "_copy_to_clipboard", lambda value: copied.append(value)
    )
    with pytest.raises(SystemExit) as error:
        exporter.main()
    assert error.value.code == 1
    assert copied == []


@pytest.mark.parametrize("module_fallback", [False, True])
def test_spaced_executable_and_profile_are_preserved_as_argv(
    exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
    module_fallback: bool,
) -> None:
    executable = (
        "C:/Program Files/Python/python.exe"
        if module_fallback
        else "C:/Program Files/yt-dlp/yt-dlp.exe"
    )
    monkeypatch.setattr(
        exporter.shutil, "which", lambda _: None if module_fallback else executable
    )
    monkeypatch.setattr(sys, "executable", executable)
    calls = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        if "--cookies" in argv:
            Path(argv[argv.index("--cookies") + 1]).write_text(COOKIE, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, stdout="synthetic-version", stderr=""
        )

    monkeypatch.setattr(exporter.subprocess, "run", run)
    command = exporter._check_ytdlp()
    assert exporter._extract_cookies(command, "chrome", "Profile 3") == COOKIE
    extraction = calls[-1]
    expected_prefix = [executable, "-m", "yt_dlp"] if module_fallback else [executable]
    assert extraction[: len(expected_prefix)] == expected_prefix
    assert (
        extraction[extraction.index("--cookies-from-browser") + 1] == "chrome:Profile 3"
    )
    assert extraction[-2:] == ["--", exporter.DUMMY_URL]


@pytest.mark.parametrize(
    "failure", ["timeout", "not-found", "missing-file", "read-error", "success"]
)
def test_cookie_temp_directory_is_cleaned_for_every_exit(
    exporter: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = exporter.tempfile.mkdtemp
    directories = []

    def mkdtemp(
        suffix: str | None = None, prefix: str | None = None, dir: str | None = None
    ) -> str:
        directory = original(suffix=suffix, prefix=prefix, dir=str(tmp_path))
        directories.append(Path(directory))
        return directory

    def run(argv: list[str], **kwargs: Any) -> Any:
        cookie_file = Path(argv[argv.index("--cookies") + 1])
        if failure != "missing-file":
            cookie_file.write_text(COOKIE, encoding="utf-8")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if failure == "not-found":
            raise FileNotFoundError("synthetic executable missing")
        return subprocess.CompletedProcess(
            argv, 1 if failure == "missing-file" else 0, stdout="", stderr=""
        )

    monkeypatch.setattr(exporter.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(exporter.subprocess, "run", run)
    if failure == "read-error":
        monkeypatch.setattr(
            exporter,
            "open",
            lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic read error")),
            raising=False,
        )
        with pytest.raises(OSError):
            exporter._extract_cookies("synthetic-yt-dlp", "firefox", None)
    else:
        content = exporter._extract_cookies("synthetic-yt-dlp", "firefox", None)
        assert content == (COOKIE if failure == "success" else None)
    assert directories
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("cancel", ["q", "eof", "interrupt"])
def test_interactive_cancellation_stops_before_cookie_extraction(
    exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
    cancel: str,
) -> None:
    monkeypatch.setattr(exporter, "_check_ytdlp", lambda: ["synthetic-yt-dlp"])
    monkeypatch.setattr(exporter, "_check_browser_running", lambda _: False)
    profiles = [
        {"dir": f"Profile {n}", "name": f"Synthetic {n}", "email": "", "gaia_name": ""}
        for n in (1, 2)
    ]
    monkeypatch.setattr(exporter, "_discover_chromium_profiles", lambda _: profiles)
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", "chrome"])
    extracted = []
    monkeypatch.setattr(
        exporter, "_extract_cookies", lambda *a: extracted.append(a) or COOKIE
    )
    monkeypatch.setattr(exporter, "_copy_to_clipboard", lambda _: False)

    def user_input(prompt: str) -> str:
        if cancel == "eof":
            raise EOFError
        if cancel == "interrupt":
            raise KeyboardInterrupt
        return "q"

    monkeypatch.setattr("builtins.input", user_input)
    with pytest.raises(SystemExit) as error:
        exporter.main()
    assert error.value.code == 0
    assert extracted == []


def test_clipboard_timeout_terminates_and_reaps_process(
    exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ClipboardProcess:
        returncode = None
        stopped = False
        reaped = False

        def communicate(self, **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired("synthetic-clip", 5)

        def kill(self) -> None:
            self.stopped = True

        def wait(self, **kwargs: Any) -> None:
            self.reaped = True

    process = ClipboardProcess()
    monkeypatch.setattr(exporter.platform, "system", lambda: "Windows")
    monkeypatch.setattr(exporter.subprocess, "Popen", lambda *a, **k: process)
    assert exporter._copy_to_clipboard("synthetic-value") is False
    assert process.stopped and process.reaped


def test_clipboard_unavailable_prints_complete_usable_env_line(
    exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(exporter, "_check_ytdlp", lambda: ["synthetic-yt-dlp"])
    monkeypatch.setattr(exporter, "_check_browser_running", lambda _: False)
    monkeypatch.setattr(exporter, "_resolve_profile", lambda *a: None)
    monkeypatch.setattr(exporter, "_extract_cookies", lambda *a: COOKIE)
    monkeypatch.setattr(exporter, "_copy_to_clipboard", lambda _: False)
    monkeypatch.setattr(sys, "argv", ["export_cookies.py", "firefox"])
    exporter.main()
    expected = (
        "YTDLP_COOKIES_B64="
        + base64.b64encode(("# Netscape HTTP Cookie File\n" + COOKIE).encode()).decode()
    )
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    "boundary", ["module", "windows-browser", "posix-browser", "clipboard"]
)
def test_unexpected_process_failures_propagate(
    exporter: Any, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Programmer errors must not disguise accidental external calls as unavailable tools."""
    monkeypatch.setattr(exporter.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        exporter.platform,
        "system",
        lambda: "Linux" if boundary == "posix-browser" else "Windows",
    )

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("synthetic unexpected process failure")

    monkeypatch.setattr(exporter.subprocess, "run", fail)
    monkeypatch.setattr(exporter.subprocess, "Popen", fail)
    with pytest.raises(AssertionError, match="synthetic unexpected process failure"):
        if boundary == "module":
            exporter._check_ytdlp()
        elif boundary == "clipboard":
            exporter._copy_to_clipboard("synthetic-value")
        else:
            exporter._check_browser_running("chrome")


@pytest.mark.parametrize(
    "boundary", ["module", "windows-browser", "posix-browser", "clipboard"]
)
@pytest.mark.parametrize("failure", ["launch", "timeout", "exit"])
def test_expected_process_failures_report_unavailable(
    exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failure: str,
) -> None:
    monkeypatch.setattr(exporter.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        exporter.platform,
        "system",
        lambda: "Linux" if boundary == "posix-browser" else "Windows",
    )

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure == "launch":
            raise OSError("synthetic process unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("synthetic-process", 5)
        raise subprocess.CalledProcessError(1, "synthetic-process")

    monkeypatch.setattr(exporter.subprocess, "run", fail)
    monkeypatch.setattr(exporter.subprocess, "Popen", fail)
    if boundary == "module":
        with pytest.raises(SystemExit) as error:
            exporter._check_ytdlp()
        assert error.value.code == 1
    elif boundary == "clipboard":
        assert exporter._copy_to_clipboard("synthetic-value") is False
    else:
        assert exporter._check_browser_running("chrome") is False
