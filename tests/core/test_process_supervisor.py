import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.process import ProcessHandle, ProcessOwnerCancelled, ProcessSupervisor


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(1):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_request_cancel_leaves_no_process_socket_or_partial_after_two_seconds(
    tmp_path: Path,
):
    """Catches cancellation that returns before the process group and artifacts die."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    partial = tmp_path / "download.part"
    ready = tmp_path / "ready"
    script = (
        "import pathlib,socket,time;"
        f"p=pathlib.Path({str(partial)!r});p.write_bytes(b'partial');"
        "s=socket.socket();"
        f"s.bind(('127.0.0.1',{port}));s.listen();"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(60)"
    )
    supervisor = ProcessSupervisor(max_processes=1)
    task = asyncio.create_task(
        supervisor.run(
            [sys.executable, "-c", script],
            owner="req-1",
            cleanup_paths=(partial,),
        )
    )
    await _wait_for_file(ready)

    started = time.monotonic()
    await supervisor.cancel_owner("req-1")
    await asyncio.wait_for(task, timeout=max(0.1, 2 - (time.monotonic() - started)))

    assert time.monotonic() - started < 2
    assert supervisor.processes_for("req-1") == ()
    assert not partial.exists()
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_supervisor_bounds_concurrent_processes(tmp_path: Path):
    """Catches a second media process starting before the shared slot is released."""
    first_ready = tmp_path / "first-ready"
    second_ready = tmp_path / "second-ready"
    supervisor = ProcessSupervisor(max_processes=1)
    first = asyncio.create_task(
        supervisor.run(
            [
                sys.executable,
                "-c",
                f"import pathlib,time;pathlib.Path({str(first_ready)!r}).touch();time.sleep(60)",
            ],
            owner="first",
        )
    )
    await _wait_for_file(first_ready)
    second = asyncio.create_task(
        supervisor.run(
            [
                sys.executable,
                "-c",
                f"import pathlib;pathlib.Path({str(second_ready)!r}).touch()",
            ],
            owner="second",
        )
    )
    await asyncio.sleep(0.1)
    assert not second_ready.exists()

    await supervisor.cancel_owner("first")
    await asyncio.wait_for(first, timeout=2)
    await asyncio.wait_for(second, timeout=2)
    assert second_ready.exists()


@pytest.mark.asyncio
async def test_nested_processes_for_same_owner_share_one_bounded_lease():
    """A two-process pipe must not deadlock when the configured limit is one."""
    supervisor = ProcessSupervisor(max_processes=1)

    async with asyncio.timeout(2):
        async with supervisor.open([sys.executable, "-c", "import time;time.sleep(1)"]):
            result = await supervisor.run([sys.executable, "-c", "print('child')"])

    assert result.returncode == 0
    assert result.stdout.strip() == b"child"


@pytest.mark.asyncio
async def test_scoped_owner_cancels_implicit_child_process(tmp_path: Path):
    from app.core.process import process_owner_scope

    ready = tmp_path / "scoped-ready"
    supervisor = ProcessSupervisor(max_processes=1)
    with process_owner_scope("request-token"):
        task = asyncio.create_task(
            supervisor.run(
                [
                    sys.executable,
                    "-c",
                    f"import pathlib,time;pathlib.Path({str(ready)!r}).touch();time.sleep(60)",
                ]
            )
        )
    await _wait_for_file(ready)

    await supervisor.cancel_owner("request-token")
    await asyncio.wait_for(task, timeout=2)

    assert supervisor.processes_for("request-token") == ()


@pytest.mark.asyncio
async def test_explicit_owner_overrides_scoped_owner(tmp_path: Path):
    from app.core.process import process_owner_scope

    ready = tmp_path / "explicit-ready"
    supervisor = ProcessSupervisor(max_processes=1)
    with process_owner_scope("request-token"):
        task = asyncio.create_task(
            supervisor.run(
                [
                    sys.executable,
                    "-c",
                    f"import pathlib,time;pathlib.Path({str(ready)!r}).touch();time.sleep(60)",
                ],
                owner="explicit-owner",
            )
        )
    await _wait_for_file(ready)

    await supervisor.cancel_owner("request-token")
    assert not task.done()
    await supervisor.cancel_owner("explicit-owner")
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_cancel_owner_does_not_cancel_its_calling_task():
    from app.core.process import process_owner_scope, process_supervisor

    current = asyncio.current_task()
    assert current is not None
    with process_owner_scope("cancel-caller"):
        await asyncio.wait_for(
            process_supervisor.cancel_owner("cancel-caller"), timeout=2
        )

    assert current.cancelling() == 0


@pytest.mark.asyncio
async def test_nested_owner_scope_remains_registered_until_outer_exit():
    from app.core.process import process_owner_scope, process_supervisor

    ready = asyncio.Event()

    async def request() -> None:
        with process_owner_scope("nested-request"):
            with process_owner_scope("nested-request"):
                await asyncio.sleep(0)
            ready.set()
            await asyncio.Future()

    task = asyncio.create_task(request())
    await asyncio.wait_for(ready.wait(), timeout=1)
    await process_supervisor.cancel_owner("nested-request")

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_token_cancels_long_gallery_dl_service_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.core.process import process_owner_scope
    from app.services.gallery_dl import service as gallery_service

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ready = tmp_path / "gallery-ready"
    supervisor = ProcessSupervisor(max_processes=1)

    class GalleryProcessProxy:
        def run(self, _command, **kwargs):
            output_dir = Path(tuple(kwargs["cleanup_paths"])[0])
            script = (
                "import pathlib,socket,time;"
                f"pathlib.Path({str(output_dir / 'item.part')!r}).touch();"
                "s=socket.socket();"
                f"s.bind(('127.0.0.1',{port}));s.listen();"
                f"pathlib.Path({str(ready)!r}).touch();"
                "time.sleep(60)"
            )
            return supervisor.run([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(gallery_service, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(gallery_service, "process_supervisor", GalleryProcessProxy())
    with process_owner_scope("gallery-token"):
        task = asyncio.create_task(
            gallery_service.GalleryDlService.download_video(
                "https://example.invalid/video"
            )
        )
    await _wait_for_file(ready)

    started = time.monotonic()
    await supervisor.cancel_owner("gallery-token")
    await asyncio.wait_for(task, timeout=max(0.1, 2 - (time.monotonic() - started)))

    assert not list(tmp_path.glob("gdl_video_*"))
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_token_cancels_long_ffmpeg_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.core.process import process_owner_scope
    from app.services import converter

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ready = tmp_path / "ffmpeg-ready"
    source = tmp_path / "audio.webm"
    output = tmp_path / "audio.mp3"
    source.write_bytes(b"source")
    supervisor = ProcessSupervisor(max_processes=1)

    class FfmpegProcessProxy:
        def run(self, _command, **kwargs):
            script = (
                "import pathlib,socket,time;"
                f"pathlib.Path({str(output)!r}).write_bytes(b'partial');"
                "s=socket.socket();"
                f"s.bind(('127.0.0.1',{port}));s.listen();"
                f"pathlib.Path({str(ready)!r}).touch();"
                "time.sleep(60)"
            )
            return supervisor.run([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(converter, "process_supervisor", FfmpegProcessProxy())
    with process_owner_scope("ffmpeg-token"):
        task = asyncio.create_task(converter.MediaConverter.convert_to_mp3(str(source)))
    await _wait_for_file(ready)

    started = time.monotonic()
    await supervisor.cancel_owner("ffmpeg-token")
    result = await asyncio.wait_for(
        task, timeout=max(0.1, 2 - (time.monotonic() - started))
    )

    assert result is None
    assert not output.exists()
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", port))


@pytest.mark.asyncio
async def test_same_owner_nested_processes_have_a_separate_hard_bound(tmp_path: Path):
    """Nested pipeline capacity must be finite even when its owner is shared."""
    supervisor = ProcessSupervisor(max_processes=1, max_child_processes=2)
    ready = [tmp_path / f"ready-{index}" for index in range(3)]

    def command(index: int) -> list[str]:
        return [
            sys.executable,
            "-c",
            (
                f"import pathlib,time;pathlib.Path({str(ready[index])!r}).touch();"
                "time.sleep(0.5)"
            ),
        ]

    tasks = [
        asyncio.create_task(supervisor.run(command(index), owner="pipeline"))
        for index in range(3)
    ]
    try:
        await _wait_for_file(ready[0])
        await _wait_for_file(ready[1])
        await asyncio.sleep(0.1)
        assert not ready[2].exists()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert ready[2].exists()
    finally:
        await supervisor.cancel_owner("pipeline")
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_ytdlp_owned_stem_cleanup_removes_format_partials(tmp_path: Path):
    output = tmp_path / "job.mp4"
    partial = tmp_path / "job.f137.mp4.part"
    ready = tmp_path / "stem-ready"
    script = (
        "import pathlib,time;"
        f"pathlib.Path({str(partial)!r}).write_bytes(b'partial');"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(60)"
    )
    supervisor = ProcessSupervisor(max_processes=1)
    task = asyncio.create_task(
        supervisor.run(
            [sys.executable, "-c", script, "--output", str(output)],
            owner="stem-cleanup",
        )
    )
    await _wait_for_file(ready)

    await supervisor.cancel_owner("stem-cleanup")
    await asyncio.wait_for(task, timeout=2)

    assert not partial.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_windows_job_kills_child_after_group_leader_exits(tmp_path: Path):
    """The retained tree owner must survive the original PID exiting first."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    ready = tmp_path / "child-ready"
    child_pid = tmp_path / "child.pid"
    child_script = (
        "import os,pathlib,socket,time;"
        "s=socket.socket();"
        f"s.bind(('127.0.0.1',{port}));s.listen();"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()));"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(60)"
    )
    parent_script = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}])"
    )
    supervisor = ProcessSupervisor(max_processes=1)
    task = asyncio.create_task(
        supervisor.run([sys.executable, "-c", parent_script], owner="tree")
    )
    try:
        await _wait_for_file(ready)
        async with asyncio.timeout(1):
            while not any(
                proc.returncode is not None for proc in supervisor.processes_for("tree")
            ):
                await asyncio.sleep(0.01)

        started = time.monotonic()
        await supervisor.cancel_owner("tree")
        await asyncio.wait_for(task, timeout=max(0.1, 2 - (time.monotonic() - started)))

        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
    finally:
        if child_pid.exists():
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", child_pid.read_text()],
                capture_output=True,
                check=False,
            )
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_windows_normal_finish_closes_job_and_kills_descendant(tmp_path: Path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    ready = tmp_path / "normal-child-ready"
    child_script = (
        "import pathlib,socket,time;"
        "s=socket.socket();"
        f"s.bind(('127.0.0.1',{port}));s.listen();"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(60)"
    )
    parent_script = (
        "import pathlib,subprocess,sys,time;"
        f"ready=pathlib.Path({str(ready)!r});"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "\nwhile not ready.exists(): time.sleep(0.01)"
    )

    supervisor = ProcessSupervisor(max_processes=1)
    result = await asyncio.wait_for(
        supervisor.run([sys.executable, "-c", parent_script], owner="normal-tree"),
        timeout=2,
    )

    assert result.returncode == 0
    async with asyncio.timeout(1):
        while True:
            try:
                with socket.socket() as replacement:
                    replacement.bind(("127.0.0.1", port))
                break
            except OSError:
                await asyncio.sleep(0.01)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_windows_assignment_failure_kills_process_and_closes_job(monkeypatch):
    proc = AsyncMock()
    proc.pid = 12345
    proc.returncode = None
    proc.kill = Mock()
    proc.wait = AsyncMock(return_value=0)
    closed: list[int] = []

    async def create_process(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("app.core.process._create_windows_job", lambda: 99)
    monkeypatch.setattr(
        "app.core.process._assign_windows_job_and_resume",
        Mock(side_effect=OSError("assign failed")),
    )
    monkeypatch.setattr(
        "app.core.process._close_windows_job", lambda handle: closed.append(handle)
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    supervisor = ProcessSupervisor(max_processes=1)
    with pytest.raises(OSError, match="assign failed"):
        await supervisor.run(["fake"], owner="failed-job")

    proc.kill.assert_called_once()
    assert closed == [99]
    assert supervisor.processes_for("failed-job") == ()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object behavior")
@pytest.mark.asyncio
async def test_windows_cancel_and_finish_serialize_job_handle_teardown(monkeypatch):
    proc = AsyncMock()
    proc.returncode = 0
    proc.wait = AsyncMock(return_value=0)
    queried = 0
    closed: list[int] = []

    def active_processes(handle: int) -> int:
        nonlocal queried
        assert handle == 99
        queried += 1
        return 1 if queried == 1 else 0

    monkeypatch.setattr("app.core.process._terminate_windows_job", lambda _job: True)
    monkeypatch.setattr(
        "app.core.process._windows_job_active_processes", active_processes
    )
    monkeypatch.setattr(
        "app.core.process._close_windows_job", lambda handle: closed.append(handle)
    )
    handle = ProcessHandle(
        proc=proc,
        owner="race",
        process_group_id=1,
        windows_job=99,
    )

    for _ in range(20):
        await asyncio.gather(handle.cancel(), handle.close_tree())

    assert closed == [99]


@pytest.mark.asyncio
async def test_cancel_owner_wins_a_process_start_race(tmp_path: Path):
    """Catches a queued launch escaping cancellation before its task is scheduled."""
    partial = tmp_path / "late.part"
    supervisor = ProcessSupervisor(max_processes=1)
    task = asyncio.create_task(
        supervisor.run(
            [
                sys.executable,
                "-c",
                f"import pathlib,time;pathlib.Path({str(partial)!r}).touch();time.sleep(60)",
            ],
            owner="req-race",
            cleanup_paths=(partial,),
        )
    )

    await supervisor.cancel_owner("req-race")

    with pytest.raises(ProcessOwnerCancelled):
        await asyncio.wait_for(task, timeout=2)
    assert supervisor.processes_for("req-race") == ()
    assert not partial.exists()
