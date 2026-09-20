import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest

from app.core.process import ProcessOwnerCancelled, ProcessSupervisor


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
        async with supervisor.open(
            [sys.executable, "-c", "import time;time.sleep(1)"]
        ):
            result = await supervisor.run(
                [sys.executable, "-c", "print('child')"]
            )

    assert result.returncode == 0
    assert result.stdout.strip() == b"child"


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
