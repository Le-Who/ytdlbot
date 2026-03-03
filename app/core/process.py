import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from app.core import state


@dataclass
class ProcessHandle:
    proc: asyncio.subprocess.Process
    stderr_data: deque[bytes]
    stderr_task: asyncio.Task | None

    async def cancel(self) -> None:
        if self.proc.returncode is not None:
            return

        import sys
        import subprocess
        import os

        try:
            if sys.platform != "win32":
                import signal

                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                )
        except Exception:
            pass

        try:
            await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            await self.proc.wait()

    async def wait(self) -> int:
        return await self.proc.wait()


@asynccontextmanager
async def run_subprocess(
    cmd: list[str],
    *,
    stdin=asyncio.subprocess.DEVNULL,
    stdout_pipe: bool = True,
    stderr_pipe: bool = True,
    merge_stderr: bool = False,
    timeout: float | None = None,
) -> AsyncIterator[ProcessHandle]:
    import sys
    import subprocess

    kwargs = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Determine stderr handling:
    # merge_stderr=True → redirect stderr to stdout (caller reads both via proc.stdout)
    # stderr_pipe=True  → separate pipe with background consumer
    if merge_stderr:
        stderr_arg = asyncio.subprocess.STDOUT
    elif stderr_pipe:
        stderr_arg = asyncio.subprocess.PIPE
    else:
        stderr_arg = asyncio.subprocess.DEVNULL

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE if stdout_pipe else asyncio.subprocess.DEVNULL,
        stderr=stderr_arg,
        **kwargs,
    )

    async with state.active_processes_lock:
        state.active_processes.add(proc)

    stderr_data: deque[bytes] = deque(maxlen=200)
    stderr_task: asyncio.Task | None = None

    if not merge_stderr and stderr_pipe and proc.stderr is not None:

        async def consume_stderr() -> None:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_data.append(line)

        stderr_task = asyncio.create_task(consume_stderr())

    handle = ProcessHandle(proc=proc, stderr_data=stderr_data, stderr_task=stderr_task)
    try:
        if timeout is None:
            yield handle
        else:
            async with asyncio.timeout(timeout):
                yield handle
    finally:
        await handle.cancel()
        if stderr_task:
            await stderr_task
        async with state.active_processes_lock:
            state.active_processes.discard(proc)
