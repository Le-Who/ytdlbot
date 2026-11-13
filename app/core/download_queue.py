"""Fair, cancellation-safe leases for bounded downloads."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Generator
from enum import Enum, auto
from typing import Self

from app.core.asyncio_utils import await_owned
from app.core.texts import Texts

logger = logging.getLogger("app.core.download_queue")


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} сек"
    return f"{int(seconds / 60)} мин"


class QueueUnavailable(RuntimeError):
    """Raised when an ``acquire`` lease cannot be granted."""


class _LeaseState(Enum):
    NEW = auto()
    ACQUIRING = auto()
    ACQUIRED = auto()
    RELEASED = auto()


class _Waiter:
    __slots__ = ("changed", "future", "notifier", "position", "state", "update_ui")

    def __init__(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
    ) -> None:
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.update_ui = update_ui
        self.position = 0
        self.state = "waiting"
        self.changed = asyncio.Event()
        self.notifier: asyncio.Task[None] | None = None


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
        self._state = _LeaseState.NEW
        self._release_task: asyncio.Task[None] | None = None

    def __await__(self) -> Generator[object, None, DownloadLease]:
        return self._enter().__await__()

    async def _enter(self) -> Self:
        if self._state is _LeaseState.RELEASED:
            raise RuntimeError("download lease has already been released")
        if self._state is _LeaseState.ACQUIRING:
            raise RuntimeError("download lease is already being acquired")
        if self._state is _LeaseState.NEW:
            self._state = _LeaseState.ACQUIRING
            try:
                acquired = await self._queue._acquire(
                    self._update_ui, kb_error=self._kb_error
                )
            except BaseException:
                self._state = _LeaseState.NEW
                raise
            if not acquired:
                self._state = _LeaseState.NEW
                raise QueueUnavailable("download queue did not grant a slot")
            self._state = _LeaseState.ACQUIRED
        return self

    async def __aenter__(self) -> Self:
        return await self._enter()

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.release()

    async def release(self) -> None:
        """Return this lease once; repeated and concurrent calls are harmless."""
        if self._state is _LeaseState.ACQUIRING:
            raise RuntimeError("download lease acquisition is still pending")
        if self._state is _LeaseState.NEW:
            return
        if self._release_task is None:
            self._state = _LeaseState.RELEASED
            self._release_task = asyncio.create_task(self._queue._release_slot())
        # Release belongs to the lease, not to any one caller. Cancellation of
        # a caller cannot abandon the slot while the queue lock is contended.
        await asyncio.shield(self._release_task)


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
        if task in self._legacy_leases:
            raise RuntimeError("task already holds a download queue lease")
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
            await self._notify_ui(update_ui, Texts.MAINTENANCE_MODE, kb_error)
            return False

        async with self._lock:
            # locked() is public. The queue lock serializes this check with all
            # grants; acquire() completes immediately while a slot is available.
            if not self._sem.locked():
                await self._sem.acquire()
                return True
            full = len(self._waiters) >= self._max_queue
            if not full:
                waiter = _Waiter(update_ui)
                self._waiters.append(waiter)
                waiter.position = len(self._waiters)

        if full:
            await self._notify_ui(update_ui, Texts.QUEUE_FULL, kb_error)
            return False

        waiter.notifier = asyncio.create_task(self._notify_waiter(waiter))
        try:
            async with asyncio.timeout(float(self._timeout)):
                await asyncio.shield(waiter.future)
            await await_owned(self._settle_notifier(waiter))
            return True
        except TimeoutError:
            await await_owned(self._withdraw_and_settle(waiter))
            await self._notify_ui(update_ui, Texts.QUEUE_TIMEOUT, kb_error)
            logger.warning("Queue waiter timed out after %ds", self._timeout)
            return False
        except BaseException:
            # If release granted the slot at the same instant cancellation won,
            # atomically forward that grant instead of losing capacity.
            await await_owned(self._withdraw_and_settle(waiter))
            raise

    async def _withdraw_and_settle(self, waiter: _Waiter) -> None:
        await self._withdraw(waiter)
        await self._settle_notifier(waiter)

    async def _settle_notifier(self, waiter: _Waiter) -> None:
        if waiter.notifier is not None:
            waiter.notifier.cancel()
            await asyncio.gather(waiter.notifier, return_exceptions=True)

    async def _notify_waiter(self, waiter: _Waiter) -> None:
        while waiter.state == "waiting":
            waiter.changed.clear()
            await self._send_position(waiter)
            await waiter.changed.wait()

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
            waiter.changed.set()

    async def _send_position(self, waiter: _Waiter) -> None:
        text = Texts.QUEUE_POSITION.format(
            pos=waiter.position,
            total=len(self._waiters),
            eta=_fmt_eta(waiter.position * self._avg_duration),
        )
        await self._notify_ui(waiter.update_ui, text, None)

    async def _notify_ui(
        self,
        update_ui: Callable[[str, object | None], Awaitable[None]],
        text: str,
        markup: object | None,
    ) -> None:
        try:
            async with asyncio.timeout(min(5.0, float(self._timeout))):
                await update_ui(text, markup)
        # Caller-supplied UI callbacks are optional; queue ownership must survive
        # any callback failure while cancellation continues to propagate.
        except Exception as exc:  # noqa: BLE001
            logger.debug("Queue position update failed: %s", exc)

    @property
    def queue_depth(self) -> int:
        return len(self._waiters)
