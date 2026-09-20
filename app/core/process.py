"""Bounded, owner-aware supervision for media subprocess groups."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from collections import deque
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Hashable,
    Iterable,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.core import state


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessOwnerCancelled(RuntimeError):
    """A pending process launch was cancelled by its request owner."""


@dataclass(eq=False)
class ProcessHandle:
    proc: asyncio.subprocess.Process
    owner: Hashable
    process_group_id: int
    cleanup_paths: tuple[Path, ...] = ()
    stderr_data: deque[bytes] = field(default_factory=lambda: deque(maxlen=200))
    stderr_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    finished: bool = False
    _cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def cancel(self) -> None:
        async with self._cancel_lock:
            if self.cancel_requested:
                return
            self.cancel_requested = True
            await self._terminate_group()
            await self._cleanup_cancelled_paths()

    async def _terminate_group(self) -> None:
        if sys.platform == "win32":
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    capture_output=True,
                    timeout=0.5,
                )
                if completed.returncode != 0 and self.proc.returncode is None:
                    self.proc.kill()
            except (OSError, subprocess.SubprocessError):
                if self.proc.returncode is None:
                    try:
                        self.proc.kill()
                    except ProcessLookupError:
                        pass
        else:
            try:
                os.killpg(self.process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=0.5)
                return
            except TimeoutError:
                try:
                    os.killpg(self.process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            await asyncio.wait_for(self.proc.wait(), timeout=0.5)
        except TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=0.2)
            except TimeoutError:
                # The process has received the strongest available kill. Never
                # let a broken platform wait primitive violate cancellation's
                # bounded completion contract.
                pass

    async def _cleanup_cancelled_paths(self) -> None:
        for path in self.cleanup_paths:
            try:
                if path.is_dir():
                    await asyncio.to_thread(shutil.rmtree, path, True)
                else:
                    # Downloaders commonly add .part/.ytdl/fragment suffixes to
                    # the configured output path.  The base names are UUIDs, so
                    # removing the owned prefix is both complete and isolated.
                    for candidate in path.parent.glob(f"{path.name}*"):
                        if candidate.is_dir():
                            await asyncio.to_thread(shutil.rmtree, candidate, True)
                        else:
                            candidate.unlink(missing_ok=True)
            except OSError:
                pass

    async def wait(self) -> int:
        return await self.proc.wait()


class ProcessSupervisor:
    """Own and bound subprocesses across yt-dlp, gallery-dl and FFmpeg tools."""

    def __init__(self, max_processes: int = 4) -> None:
        if max_processes < 1:
            raise ValueError("max_processes must be positive")
        self._slots = asyncio.Semaphore(max_processes)
        self._lock = asyncio.Lock()
        self._by_owner: dict[Hashable, set[ProcessHandle]] = {}
        self._owner_generation: dict[Hashable, int] = {}
        self._owner_slot_refs: dict[Hashable, int] = {}

    @staticmethod
    def _owner(owner: Hashable | None) -> Hashable:
        if owner is not None:
            return owner
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("a process owner is required outside an asyncio task")
        return task

    @staticmethod
    def _owned_output_paths(command: Sequence[str]) -> tuple[Path, ...]:
        """Find concrete yt-dlp output paths that require cancellation cleanup."""
        paths: list[Path] = []
        for index, argument in enumerate(command):
            output: str | None = None
            if argument in {"-o", "--output"} and index + 1 < len(command):
                output = command[index + 1]
            elif argument.startswith("--output="):
                output = argument.partition("=")[2]
            if output and output != "-" and "%" not in output:
                paths.append(Path(output))
        return tuple(paths)

    async def _start(
        self,
        command: Sequence[str],
        *,
        owner: Hashable | None,
        cleanup_paths: Iterable[os.PathLike[str] | str],
        stdin: int | None,
        stdout_pipe: bool,
        stderr_pipe: bool,
        owner_generation: int | None = None,
    ) -> ProcessHandle:
        owner_key = self._owner(owner)
        generation = (
            self._owner_generation.get(owner_key, 0)
            if owner_generation is None
            else owner_generation
        )
        await self._acquire_owner_slot(owner_key)
        proc: asyncio.subprocess.Process | None = None
        handle: ProcessHandle | None = None
        try:
            async with self._lock:
                cancelled = self._owner_generation.get(owner_key, 0) != generation
            if cancelled:
                raise ProcessOwnerCancelled(
                    f"process owner {owner_key!r} was cancelled"
                )

            kwargs: dict[str, object] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=stdin,
                stdout=(
                    asyncio.subprocess.PIPE
                    if stdout_pipe
                    else asyncio.subprocess.DEVNULL
                ),
                stderr=(
                    asyncio.subprocess.PIPE
                    if stderr_pipe
                    else asyncio.subprocess.DEVNULL
                ),
                **kwargs,  # type: ignore[arg-type]
            )
            handle = ProcessHandle(
                proc,
                owner_key,
                proc.pid,
                tuple(Path(path) for path in cleanup_paths)
                + self._owned_output_paths(command),
            )
            async with state.active_processes_lock:
                state.active_processes.add(proc)
            async with self._lock:
                cancelled = self._owner_generation.get(owner_key, 0) != generation
                if not cancelled:
                    self._by_owner.setdefault(owner_key, set()).add(handle)
            if cancelled:
                await handle.cancel()
                raise ProcessOwnerCancelled(
                    f"process owner {owner_key!r} was cancelled"
                )
            return handle
        except BaseException:
            if handle is not None:
                await asyncio.shield(handle.cancel())
                await asyncio.shield(self._finish(handle))
            else:
                if proc is not None and proc.returncode is None:
                    temporary = ProcessHandle(proc, owner_key, proc.pid)
                    await asyncio.shield(temporary.cancel())
                await asyncio.shield(self._release_owner_slot(owner_key))
            raise

    async def _acquire_owner_slot(self, owner: Hashable) -> None:
        """Acquire one bounded work lease, shared by nested owner processes."""
        async with self._lock:
            if owner in self._owner_slot_refs:
                self._owner_slot_refs[owner] += 1
                return

        await self._slots.acquire()
        async with self._lock:
            if owner in self._owner_slot_refs:
                self._owner_slot_refs[owner] += 1
                self._slots.release()
            else:
                self._owner_slot_refs[owner] = 1

    async def _release_owner_slot(self, owner: Hashable) -> None:
        release_slot = False
        async with self._lock:
            references = self._owner_slot_refs.get(owner, 0)
            if references <= 1:
                self._owner_slot_refs.pop(owner, None)
                release_slot = references == 1
            else:
                self._owner_slot_refs[owner] = references - 1
        if release_slot:
            self._slots.release()

    async def _finish(self, handle: ProcessHandle) -> None:
        first_finish = False
        async with self._lock:
            if not handle.finished:
                first_finish = True
                handle.finished = True
                owned = self._by_owner.get(handle.owner)
                if owned is not None:
                    owned.discard(handle)
                    if not owned:
                        self._by_owner.pop(handle.owner, None)
        async with state.active_processes_lock:
            state.active_processes.discard(handle.proc)
        if first_finish:
            await self._release_owner_slot(handle.owner)

    def run(
        self,
        command: Sequence[str],
        *,
        owner: Hashable | None = None,
        cleanup_paths: Iterable[os.PathLike[str] | str] = (),
        stdin: int | None = asyncio.subprocess.DEVNULL,
        stdout_pipe: bool = True,
        stderr_pipe: bool = True,
        timeout: float | None = None,
    ) -> Coroutine[object, object, ProcessResult]:
        # Capture the owner's generation when run() is called, before a task
        # created from the returned coroutine can race cancel_owner().
        owner_key = self._owner(owner)
        generation = self._owner_generation.get(owner_key, 0)
        return self._run(
            command,
            owner=owner_key,
            owner_generation=generation,
            cleanup_paths=cleanup_paths,
            stdin=stdin,
            stdout_pipe=stdout_pipe,
            stderr_pipe=stderr_pipe,
            timeout=timeout,
        )

    async def _run(
        self,
        command: Sequence[str],
        *,
        owner: Hashable,
        owner_generation: int,
        cleanup_paths: Iterable[os.PathLike[str] | str],
        stdin: int | None,
        stdout_pipe: bool,
        stderr_pipe: bool,
        timeout: float | None,
    ) -> ProcessResult:
        handle = await self._start(
            command,
            owner=owner,
            owner_generation=owner_generation,
            cleanup_paths=cleanup_paths,
            stdin=stdin,
            stdout_pipe=stdout_pipe,
            stderr_pipe=stderr_pipe,
        )
        try:
            communication = handle.proc.communicate()
            if timeout is None:
                stdout, stderr = await communication
            else:
                stdout, stderr = await asyncio.wait_for(communication, timeout)
            return ProcessResult(
                handle.proc.returncode or 0, stdout or b"", stderr or b""
            )
        except BaseException:
            await asyncio.shield(handle.cancel())
            raise
        finally:
            await asyncio.shield(self._finish(handle))

    @asynccontextmanager
    async def open(
        self,
        command: Sequence[str],
        *,
        owner: Hashable | None = None,
        cleanup_paths: Iterable[os.PathLike[str] | str] = (),
        stdin: int | None = asyncio.subprocess.DEVNULL,
        stdout_pipe: bool = True,
        stderr_pipe: bool = True,
        timeout: float | None = None,
        stderr_callback: Callable[[bytes], None] | None = None,
    ) -> AsyncIterator[ProcessHandle]:
        handle = await self._start(
            command,
            owner=owner,
            cleanup_paths=cleanup_paths,
            stdin=stdin,
            stdout_pipe=stdout_pipe,
            stderr_pipe=stderr_pipe,
        )
        if stderr_pipe and handle.proc.stderr is not None:

            async def consume_stderr() -> None:
                assert handle.proc.stderr is not None
                while line := await handle.proc.stderr.readline():
                    handle.stderr_data.append(line)
                    if stderr_callback is not None:
                        try:
                            stderr_callback(line)
                        except Exception:
                            pass

            handle.stderr_task = asyncio.create_task(consume_stderr())
        try:
            if timeout is None:
                yield handle
            else:
                async with asyncio.timeout(timeout):
                    yield handle
        finally:
            if handle.proc.returncode is None:
                await asyncio.shield(handle.cancel())
            if handle.stderr_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(handle.stderr_task), 0.2)
                except TimeoutError:
                    handle.stderr_task.cancel()
            await asyncio.shield(self._finish(handle))

    async def cancel_owner(self, owner: Hashable) -> None:
        async with self._lock:
            self._owner_generation[owner] = self._owner_generation.get(owner, 0) + 1
            handles = tuple(self._by_owner.get(owner, ()))
        if handles:
            await asyncio.gather(*(handle.cancel() for handle in handles))
            await asyncio.gather(*(self._finish(handle) for handle in handles))

    def processes_for(self, owner: Hashable) -> tuple[asyncio.subprocess.Process, ...]:
        return tuple(handle.proc for handle in self._by_owner.get(owner, ()))


def _configured_process_limit() -> int:
    try:
        return max(1, int(os.getenv("MAX_MEDIA_PROCESSES", "4")))
    except ValueError:
        return 4


process_supervisor = ProcessSupervisor(_configured_process_limit())


@asynccontextmanager
async def run_subprocess(
    cmd: list[str],
    *,
    stdin: int = asyncio.subprocess.DEVNULL,
    stdout_pipe: bool = True,
    stderr_pipe: bool = True,
    timeout: float | None = None,
    stderr_callback: Callable[[bytes], None] | None = None,
    owner: Hashable | None = None,
    cleanup_paths: Iterable[os.PathLike[str] | str] = (),
) -> AsyncIterator[ProcessHandle]:
    async with process_supervisor.open(
        cmd,
        stdin=stdin,
        stdout_pipe=stdout_pipe,
        stderr_pipe=stderr_pipe,
        timeout=timeout,
        stderr_callback=stderr_callback,
        owner=owner,
        cleanup_paths=cleanup_paths,
    ) as handle:
        yield handle
