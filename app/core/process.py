import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

from app.core.state import active_processes, active_processes_lock


@dataclass
class ProcessHandle:
    proc: asyncio.subprocess.Process
    stderr_buffer: deque[bytes]
    _stderr_task: asyncio.Task | None

    async def iter_stdout(self) -> AsyncIterator[bytes]:
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            yield line

    async def iter_stderr(self) -> AsyncIterator[bytes]:
        if not self.proc.stderr:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            yield line

    async def cancel(self) -> None:
        if self.proc.returncode is not None:
            return
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self.proc.kill()

    async def wait(self) -> int:
        return await self.proc.wait()

    @property
    def exit_code(self) -> int | None:
        return self.proc.returncode


@asynccontextmanager
async def run_subprocess(
    cmd: Iterable[str],
    *,
    stdout_pipe: bool = True,
    stderr_pipe: bool = True,
    timeout: float | None = None,
) -> AsyncIterator[ProcessHandle]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if stdout_pipe else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE if stderr_pipe else asyncio.subprocess.DEVNULL,
    )

    async with active_processes_lock:
        active_processes.add(proc)

    stderr_buffer: deque[bytes] = deque(maxlen=200)
    stderr_task: asyncio.Task | None = None

    if stderr_pipe:
        async def consume_stderr() -> None:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_buffer.append(line)

        stderr_task = asyncio.create_task(consume_stderr())

    timeout_task: asyncio.Task | None = None
    if timeout is not None:
        async def enforce_timeout() -> None:
            await asyncio.sleep(timeout)
            if proc.returncode is None:
                proc.terminate()

        timeout_task = asyncio.create_task(enforce_timeout())

    handle = ProcessHandle(proc=proc, stderr_buffer=stderr_buffer, _stderr_task=stderr_task)

    try:
        yield handle
    finally:
        if timeout_task:
            timeout_task.cancel()
        if proc.returncode is None:
            await handle.cancel()
        await proc.wait()
        if stderr_task:
            await stderr_task
        async with active_processes_lock:
            active_processes.discard(proc)
