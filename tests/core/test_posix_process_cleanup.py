"""Process-group ownership regressions, including Windows-hosted POSIX fakes."""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import process


class _ProcessGroup:
    """Model a child that ignores TERM after its group leader has exited."""

    def __init__(self, *, leader_exited: bool = False):
        self.pid = 12345
        self.returncode = 0 if leader_exited else None
        self.child_alive = True
        self.signals: list[int] = []

    def killpg(self, group_id: int, signum: int) -> None:
        assert group_id == self.pid
        if not self.child_alive:
            raise ProcessLookupError(group_id)
        self.signals.append(signum)
        if signum == 15:
            self.returncode = -15
        elif signum == 9:
            self.child_alive = False

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def posix_boundary(monkeypatch):
    def install(group: _ProcessGroup) -> None:
        # Replace this module's platform bridge without changing pytest's host OS.
        monkeypatch.setattr(process, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(process, "os", SimpleNamespace(killpg=group.killpg))
        monkeypatch.setattr(process, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))

    return install


@pytest.mark.parametrize("leader_exited", [False, True])
async def test_cancel_kills_remaining_descendants_before_removing_owned_files(
    tmp_path, posix_boundary, monkeypatch, leader_exited
):
    group = _ProcessGroup(leader_exited=leader_exited)
    posix_boundary(group)
    partial = tmp_path / "owned.part"
    partial.write_bytes(b"partial")
    real_unlink = Path.unlink

    def unlink_after_tree_exit(path, *args, **kwargs):
        assert not group.child_alive, "owned files removed while child still runs"
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_after_tree_exit)
    handle = process.ProcessHandle(
        group, "request", group.pid, cleanup_paths=(partial,)
    )

    await handle.cancel()
    await handle.close_tree()

    assert not group.child_alive
    assert not partial.exists()


async def test_normal_finish_kills_descendants_and_preserves_completed_output(
    tmp_path, posix_boundary
):
    group = _ProcessGroup(leader_exited=True)
    posix_boundary(group)
    output = tmp_path / "completed.mp4"
    output.write_bytes(b"completed media")
    handle = process.ProcessHandle(group, "request", group.pid, cleanup_paths=(output,))

    await handle.close_tree()
    await handle.close_tree()

    assert not group.child_alive
    assert output.read_bytes() == b"completed media"
    assert group.signals == [9]


async def test_closing_an_already_exited_group_is_idempotent(posix_boundary):
    group = _ProcessGroup(leader_exited=True)
    group.child_alive = False
    posix_boundary(group)
    handle = process.ProcessHandle(group, "request", group.pid)

    await handle.cancel()
    await handle.close_tree()
    await handle.close_tree()

    assert group.signals == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="requires real POSIX process groups"
)
@pytest.mark.parametrize("cancel", [False, True], ids=["normal-finish", "cancel"])
async def test_real_posix_descendant_releases_socket_after_leader_exit(
    tmp_path, cancel
):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ready = tmp_path / "child-ready"
    child_pid = tmp_path / "child-pid"
    release = tmp_path / "release-parent"
    child_script = (
        "import os,pathlib,signal,socket,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "s=socket.socket();"
        f"s.bind(('127.0.0.1',{port}));s.listen();"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()));"
        f"pathlib.Path({str(ready)!r}).touch();"
        "time.sleep(60)"
    )
    child_output = "None" if cancel else "subprocess.DEVNULL"
    parent_script = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}],"
        f"stdin=subprocess.DEVNULL,stdout={child_output},stderr={child_output});"
        f"release=pathlib.Path({str(release)!r});"
        "\nwhile not release.exists(): time.sleep(0.01)"
    )
    supervisor = process.ProcessSupervisor(max_processes=1)
    task = asyncio.create_task(
        supervisor.run([sys.executable, "-c", parent_script], owner="posix-tree")
    )
    try:
        async with asyncio.timeout(10):
            while not ready.exists():
                if task.done():
                    pytest.fail(f"parent exited before child setup: {await task}")
                await asyncio.sleep(0.01)
        if cancel:
            release.touch()
            async with asyncio.timeout(2):
                while any(
                    p.returncode is None for p in supervisor.processes_for("posix-tree")
                ):
                    await asyncio.sleep(0.01)
            await supervisor.cancel_owner("posix-tree")
        else:
            release.touch()
        await asyncio.wait_for(task, timeout=2)
        async with asyncio.timeout(1):
            while True:
                try:
                    with socket.socket() as replacement:
                        replacement.bind(("127.0.0.1", port))
                    break
                except OSError:
                    await asyncio.sleep(0.01)
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
