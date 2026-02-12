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

    @property
    def exit_code(self) -> int | None:
        return self.proc.returncode

    async def cancel(self) -> None:
        if self.proc.returncode is not None:
            return
        try:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        except Exception:
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
    stdout_pipe: bool = True,
    stderr_pipe: bool = True,
    timeout: float | None = None,
) -> AsyncIterator[ProcessHandle]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if stdout_pipe else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE if stderr_pipe else asyncio.subprocess.DEVNULL,
    )

    async with state.active_processes_lock:
        state.active_processes.add(proc)

    stderr_data: deque[bytes] = deque(maxlen=200)
    stderr_task: asyncio.Task | None = None

    if stderr_pipe and proc.stderr is not None:
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
