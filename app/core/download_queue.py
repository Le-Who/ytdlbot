"""Fair, cancellation-safe leases for bounded downloads."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Generator
from typing import Self

from app.core.texts import Texts

logger = logging.getLogger("app.core.download_queue")


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} сек"
    return f"{int(seconds / 60)} мин"


class QueueUnavailable(RuntimeError):
    """Raised when an ``acquire`` lease cannot be granted."""


class _Waiter:
    __slots__ = ("future", "position", "state", "update_ui")

    def __init__(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
    ) -> None:
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.update_ui = update_ui
        self.position = 0
        self.state = "waiting"


class DownloadLease:
    """One idempotently releasable queue slot.

    A lease is both awaitable and an asynchronous context manager, allowing
    ``lease = await queue.acquire(...)`` and ``async with queue.acquire(...)``.
    """

    def __init__(
        self,
        queue: DownloadQueue,
        update_ui: Callable[[str, object | None], Awaitable[None]],
        kb_error: object | None,
    ) -> None:
        self._queue = queue
        self._update_ui = update_ui
        self._kb_error = kb_error
        self._acquired = False
        self._released = False

    def __await__(self) -> Generator[object, None, DownloadLease]:
        return self._enter().__await__()

    async def _enter(self) -> Self:
        if self._released:
            raise RuntimeError("download lease has already been released")
        if not self._acquired:
            acquired = await self._queue._acquire(
                self._update_ui, kb_error=self._kb_error
            )
            if not acquired:
                raise QueueUnavailable("download queue did not grant a slot")
            self._acquired = True
        return self

    async def __aenter__(self) -> Self:
        return await self._enter()

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.release()

    async def release(self) -> None:
        """Return this lease once; repeated and concurrent calls are harmless."""
        if not self._acquired or self._released:
            return
        self._released = True
        await self._queue._release_slot()


class DownloadQueue:
    """Fair queue layered on the public ``asyncio.Semaphore`` API."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        max_queue_size: int = 15,
        timeout_seconds: int = 300,
        avg_task_seconds: int = 45,
    ) -> None:
        self._sem = semaphore
        self._max_queue = max_queue_size
        self._timeout = timeout_seconds
        self._avg_duration = avg_task_seconds
        self._waiters: list[_Waiter] = []
        self._lock = asyncio.Lock()
        self._legacy_leases: dict[asyncio.Task[object], DownloadLease] = {}

    def acquire(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
        *,
        kb_error: object | None = None,
    ) -> DownloadLease:
        return DownloadLease(self, update_ui, kb_error)

    async def enqueue(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
        *,
        kb_error: object | None = None,
    ) -> bool:
        """Backward-compatible boolean acquisition for existing handlers."""
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("DownloadQueue.enqueue requires an asyncio task")
        lease = self.acquire(update_ui, kb_error=kb_error)
        try:
            await lease
        except QueueUnavailable:
            return False
        self._legacy_leases[task] = lease
        return True

    def release(self) -> None:
        """Backward-compatible idempotent release for the current task."""
        task = asyncio.current_task()
        if task is None:
            return
        lease = self._legacy_leases.pop(task, None)
        if lease is not None:
            asyncio.get_running_loop().create_task(lease.release())

    async def _acquire(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
        *,
        kb_error: object | None,
    ) -> bool:
        import app.core.state as _state

        if _state.disk_critical:
            await update_ui(Texts.MAINTENANCE_MODE, kb_error)
            return False

        async with self._lock:
            # locked() is public. The queue lock serializes this check with all
            # grants; acquire() completes immediately while a slot is available.
            if not self._sem.locked():
                await self._sem.acquire()
                return True
            if len(self._waiters) >= self._max_queue:
                await update_ui(Texts.QUEUE_FULL, kb_error)
                return False
            waiter = _Waiter(update_ui)
            self._waiters.append(waiter)
            waiter.position = len(self._waiters)

        try:
            await self._send_position(waiter)
            await asyncio.wait_for(
                asyncio.shield(waiter.future), timeout=float(self._timeout)
            )
            return True
        except TimeoutError:
            await self._withdraw(waiter)
            await update_ui(Texts.QUEUE_TIMEOUT, kb_error)
            logger.warning("Queue waiter timed out after %ds", self._timeout)
            return False
        except BaseException:
            # If release granted the slot at the same instant cancellation won,
            # atomically forward that grant instead of losing capacity.
            await asyncio.shield(self._withdraw(waiter))
            raise

    async def _withdraw(self, waiter: _Waiter) -> None:
        async with self._lock:
            if waiter.state == "waiting":
                waiter.state = "cancelled"
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
                self._reposition_locked()
            elif waiter.state == "granted":
                waiter.state = "cancelled"
                self._handoff_locked()

    async def _release_slot(self) -> None:
        async with self._lock:
            self._handoff_locked()

    def _handoff_locked(self) -> None:
        while self._waiters:
            waiter = self._waiters.pop(0)
            if waiter.state != "waiting" or waiter.future.cancelled():
                waiter.state = "cancelled"
                continue
            waiter.state = "granted"
            self._reposition_locked()
            waiter.future.set_result(None)
            return
        self._sem.release()

    def _reposition_locked(self) -> None:
        for index, waiter in enumerate(self._waiters, start=1):
            waiter.position = index
            asyncio.get_running_loop().create_task(self._send_position(waiter))

    async def _send_position(self, waiter: _Waiter) -> None:
        text = Texts.QUEUE_POSITION.format(
            pos=waiter.position,
            total=len(self._waiters),
            eta=_fmt_eta(waiter.position * self._avg_duration),
        )
        try:
            await waiter.update_ui(text, None)
        except Exception as exc:
            logger.debug("Queue position update failed: %s", exc)

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)
