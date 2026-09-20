"""Bounded, owner-aware supervision for media subprocess groups."""

from __future__ import annotations

import asyncio
import ctypes
import logging
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
    Iterator,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from app.core import state

logger = logging.getLogger("app.core.process")

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_OWNER_CANCEL_TIMEOUT_SECONDS = 1.5
_process_owner: ContextVar[Hashable | None] = ContextVar(
    "media_process_owner", default=None
)


@contextmanager
def process_owner_scope(owner: Hashable | None) -> Iterator[None]:
    """Bind one request owner to its task and every nested subprocess."""
    if owner is None:
        yield
        return
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    binding = _process_owner.set(owner)
    if task is not None:
        process_supervisor._bind_owner_task(owner, task)
    try:
        yield
    finally:
        if task is not None:
            process_supervisor._unbind_owner_task(owner, task)
        _process_owner.reset(binding)


def current_process_owner() -> Hashable | None:
    return _process_owner.get()


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _create_windows_job() -> int:
    kernel32 = _windows_kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    if not configured:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return int(job)


def _windows_process_handle(proc: asyncio.subprocess.Process) -> int:
    transport = getattr(proc, "_transport")
    popen = transport.get_extra_info("subprocess")
    return int(popen._handle)


def _assign_windows_job_and_resume(
    job_handle: int, proc: asyncio.subprocess.Process
) -> None:
    kernel32 = _windows_kernel32()
    process_handle = _windows_process_handle(proc)
    if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(process_handle)
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS {status:#x}")


def _terminate_windows_job(job_handle: int) -> bool:
    return bool(_windows_kernel32().TerminateJobObject(job_handle, 1))


def _windows_job_active_processes(job_handle: int) -> int:
    information = _BasicAccountingInformation()
    queried = _windows_kernel32().QueryInformationJobObject(
        job_handle,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    )
    if not queried:
        return 0
    return int(information.ActiveProcesses)


def _close_windows_job(job_handle: int) -> None:
    _windows_kernel32().CloseHandle(job_handle)


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
    cleanup_stems: tuple[Path, ...] = ()
    windows_job: int | None = None
    stderr_data: deque[bytes] = field(default_factory=lambda: deque(maxlen=200))
    stderr_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    finished: bool = False
    _tree_closed: bool = False
    _teardown_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def cancel(self) -> None:
        async with self._teardown_lock:
            if self.cancel_requested:
                return
            self.cancel_requested = True
            await self._terminate_group()
            await self._cleanup_cancelled_paths()

    async def _terminate_group(self) -> None:
        if sys.platform == "win32":
            if self.windows_job is not None and _terminate_windows_job(
                self.windows_job
            ):
                await self._wait_for_windows_job_exit()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                return
            if self.proc.returncode is not None:
                return
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
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for output in self.cleanup_stems:
            # yt-dlp inserts format identifiers between the configured UUID
            # stem and extension (for example job.f137.mp4.part).  Matching
            # only "<stem>.*" removes that job without touching job-other.
            try:
                for candidate in output.parent.glob(f"{output.stem}.*"):
                    if candidate.is_dir():
                        await asyncio.to_thread(shutil.rmtree, candidate, True)
                    else:
                        candidate.unlink(missing_ok=True)
            except OSError:
                pass

    async def _wait_for_windows_job_exit(self) -> None:
        if self.windows_job is None:
            return
        deadline = asyncio.get_running_loop().time() + 0.5
        while _windows_job_active_processes(self.windows_job):
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(0.01)

    async def close_tree(self) -> None:
        async with self._teardown_lock:
            if self._tree_closed:
                return
            if self.windows_job is not None:
                _terminate_windows_job(self.windows_job)
                await self._wait_for_windows_job_exit()
                _close_windows_job(self.windows_job)
                self.windows_job = None
            self._tree_closed = True

    async def wait(self) -> int:
        return await self.proc.wait()


class ProcessSupervisor:
    """Bound media workloads and their finite nested subprocess pipelines."""

    def __init__(
        self,
        max_processes: int = 4,
        *,
        max_child_processes: int | None = None,
    ) -> None:
        if max_processes < 1:
            raise ValueError("max_processes must be positive")
        child_limit = (
            max_processes * 2 if max_child_processes is None else max_child_processes
        )
        if child_limit < 1:
            raise ValueError("max_child_processes must be positive")
        # A workload lease prevents unrelated jobs from bypassing the limit.
        # A separate process lease permits the one required two-process pipe,
        # while still imposing a hard upper bound on same-owner children.
        self._workload_slots = asyncio.Semaphore(max_processes)
        self._process_slots = asyncio.Semaphore(child_limit)
        self._lock = asyncio.Lock()
        self._by_owner: dict[Hashable, set[ProcessHandle]] = {}
        self._owner_generation: dict[Hashable, int] = {}
        self._owner_slot_refs: dict[Hashable, int] = {}
        self._owner_tasks: dict[Hashable, dict[asyncio.Task[object], int]] = {}

    def _bind_owner_task(self, owner: Hashable, task: asyncio.Task[object]) -> None:
        tasks = self._owner_tasks.setdefault(owner, {})
        tasks[task] = tasks.get(task, 0) + 1

    def _unbind_owner_task(self, owner: Hashable, task: asyncio.Task[object]) -> None:
        tasks = self._owner_tasks.get(owner)
        if tasks is None:
            return
        references = tasks.get(task, 0)
        if references <= 1:
            tasks.pop(task, None)
        else:
            tasks[task] = references - 1
        if not tasks:
            self._owner_tasks.pop(owner, None)

    @staticmethod
    def _owner(owner: Hashable | None) -> Hashable:
        if owner is not None:
            return owner
        bound_owner = current_process_owner()
        if bound_owner is not None:
            return bound_owner
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
        windows_job: int | None = None
        windows_job_ready = False
        process_slot_acquired = False
        try:
            await self._process_slots.acquire()
            process_slot_acquired = True
            async with self._lock:
                cancelled = self._owner_generation.get(owner_key, 0) != generation
            if cancelled:
                raise ProcessOwnerCancelled(
                    f"process owner {owner_key!r} was cancelled"
                )

            kwargs: dict[str, object] = {}
            if sys.platform == "win32":
                windows_job = _create_windows_job()
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
                )
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
            if windows_job is not None:
                _assign_windows_job_and_resume(windows_job, proc)
                windows_job_ready = True
            handle = ProcessHandle(
                proc=proc,
                owner=owner_key,
                process_group_id=proc.pid,
                cleanup_paths=tuple(Path(path) for path in cleanup_paths),
                cleanup_stems=self._owned_output_paths(command),
                windows_job=windows_job if windows_job_ready else None,
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
                if proc is not None:
                    temporary = ProcessHandle(
                        proc=proc,
                        owner=owner_key,
                        process_group_id=proc.pid,
                        windows_job=windows_job if windows_job_ready else None,
                    )
                    await asyncio.shield(temporary.cancel())
                    await asyncio.shield(temporary.close_tree())
                    if windows_job_ready:
                        windows_job = None
                if windows_job is not None:
                    _close_windows_job(windows_job)
                if process_slot_acquired:
                    self._process_slots.release()
                await asyncio.shield(self._release_owner_slot(owner_key))
            raise

    async def _acquire_owner_slot(self, owner: Hashable) -> None:
        """Acquire one bounded work lease, shared by nested owner processes."""
        async with self._lock:
            if owner in self._owner_slot_refs:
                self._owner_slot_refs[owner] += 1
                return

        await self._workload_slots.acquire()
        async with self._lock:
            if owner in self._owner_slot_refs:
                self._owner_slot_refs[owner] += 1
                self._workload_slots.release()
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
            self._workload_slots.release()

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
            await asyncio.shield(handle.close_tree())
            self._process_slots.release()
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
        current_task = asyncio.current_task()
        async with self._lock:
            self._owner_generation[owner] = self._owner_generation.get(owner, 0) + 1
            handles = tuple(self._by_owner.get(owner, ()))
            owner_tasks = tuple(
                task
                for task in self._owner_tasks.get(owner, ())
                if task is not current_task and not task.done()
            )
        for task in owner_tasks:
            task.cancel()

        async def cancel_handle(handle: ProcessHandle) -> None:
            await handle.cancel()
            await self._finish(handle)

        cleanup_tasks = tuple(
            asyncio.create_task(cancel_handle(handle)) for handle in handles
        )
        pending_work = (*owner_tasks, *cleanup_tasks)
        if not pending_work:
            return
        done, pending = await asyncio.wait(
            pending_work, timeout=_OWNER_CANCEL_TIMEOUT_SECONDS
        )
        completed = tuple(task for task in pending_work if task in done)
        if completed:
            await asyncio.gather(
                *completed,
                return_exceptions=True,
            )
        if pending:
            logger.warning(
                "Owner %r cancellation exceeded %.1fs (%d task(s) still unwinding)",
                owner,
                _OWNER_CANCEL_TIMEOUT_SECONDS,
                len(pending),
            )

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
