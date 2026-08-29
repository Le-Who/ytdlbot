"""
Fair, position-aware download queue.

Replaces the blunt ``semaphore.locked() → reject`` pattern with a proper
wait queue.  When all semaphore slots are busy, new arrivals are put into
an asyncio.Queue, receive live position updates, and are naturally scheduled
when a slot frees up.

Usage (drop-in at every former `sem.locked()` guard):

    acquired = await state.download_queue.enqueue(update_ui)
    if not acquired:
        return          # timed out, user was notified
    try:
        ...do work...
    finally:
        state.download_queue.release()

Design notes:
- One ``DownloadQueue`` wraps one ``asyncio.Semaphore``.
- Hard cap on queue depth (``max_queue_size``) → still rejects when truly overloaded.
- Per-waiter ``asyncio.Event`` signals when it's their turn.
- On every release, all waiters get a position-update edit (best-effort).
- Timeout: waiters that exceed ``timeout_seconds`` are silently dropped.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Optional

from app.core.texts import Texts

logger = logging.getLogger("app.core.download_queue")


def _fmt_eta(seconds: float) -> str:
    """Human-readable ETA string, e.g. '45 сек' or '2 мин'."""
    if seconds < 90:
        return f"{int(seconds)} сек"
    return f"{int(seconds / 60)} мин"


class _Waiter:
    """Represents one task waiting in the queue."""

    __slots__ = ("event", "update_ui", "position")

    def __init__(
        self,
        update_ui: Callable[[str, Optional[object]], Awaitable[None]],
    ) -> None:
        self.event: asyncio.Event = asyncio.Event()
        self.update_ui = update_ui
        self.position: int = 0  # 1-based, updated on each shift


class DownloadQueue:
    """Fair wait-queue backed by an asyncio.Semaphore.

    Args:
        semaphore:       The underlying semaphore (controls real concurrency).
        max_queue_size:  Max number of tasks that may wait before hard-rejecting.
        timeout_seconds: Max seconds a task may wait in queue.
        avg_task_seconds: Rough average task duration used for ETA estimation.
    """

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
        # Ordered list of active waiters (index 0 = next in line).
        self._waiters: deque[_Waiter] = deque()
        self._lock = asyncio.Lock()  # guards _waiters mutations

    # ── Public API ────────────────────────────────────────────────────────────

    async def enqueue(
        self,
        update_ui: Callable[[str, Optional[object]], Awaitable[None]],
        *,
        kb_error: Optional[object] = None,
    ) -> bool:
        """Try to acquire a semaphore slot, queuing if busy.

        Returns:
            True  — slot acquired, caller may proceed (must call release()).
            False — hard-rejected (queue full / timed out / maintenance).
        """
        import app.core.state as _state  # avoid circular import at module level

        # ── Maintenance mode guard ─────────────────────────────────────────
        if _state.disk_critical:
            await update_ui(Texts.MAINTENANCE_MODE, kb_error)
            return False

        # ── Fast path: slot immediately available ──────────────────────────
        if self._sem._value > 0:  # type: ignore[attr-defined]
            await self._sem.acquire()
            return True

        # ── Slow path: all slots busy, try to queue ────────────────────────
        async with self._lock:
            if len(self._waiters) >= self._max_queue:
                await update_ui(Texts.QUEUE_FULL, kb_error)
                return False

            waiter = _Waiter(update_ui)
            self._waiters.append(waiter)
            waiter.position = len(self._waiters)  # 1-based

        # Send initial position message
        await self._send_position(waiter)

        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=float(self._timeout))
        except asyncio.TimeoutError:
            # Remove from queue and notify user
            async with self._lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass  # already removed by a concurrent release
            await update_ui(Texts.QUEUE_TIMEOUT, kb_error)
            logger.warning("Queue waiter timed out after %ds", self._timeout)
            return False

        # Waiter was unblocked by release() — slot is already acquired for us.
        return True

    def release(self) -> None:
        """Release the semaphore slot and wake the next waiter."""
        # Schedule _do_release as a fire-and-forget coroutine so release()
        # can remain synchronous (matching asyncio.Semaphore.release() convention).
        asyncio.get_event_loop().create_task(self._do_release())

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _do_release(self) -> None:
        async with self._lock:
            if self._waiters:
                # Pop the first waiter and grant them the slot directly
                # (we do NOT call sem.release() + sem.acquire() to avoid races).
                next_waiter = self._waiters.popleft()
                # Update positions for remaining waiters
                for idx, w in enumerate(self._waiters, start=1):
                    w.position = idx
                # Signal the next waiter — they already hold the slot conceptually.
                next_waiter.event.set()
                # Fire-and-forget position updates for remaining waiters
                for w in self._waiters:
                    asyncio.get_event_loop().create_task(self._send_position(w))
            else:
                # No one waiting — release back to semaphore normally
                self._sem.release()

    async def _send_position(self, waiter: _Waiter) -> None:
        """Send (or edit) the queue position message for a waiter."""
        total = len(self._waiters)
        # ETA = position * avg_duration (rough but honest)
        eta_secs = waiter.position * self._avg_duration
        text = Texts.QUEUE_POSITION.format(
            pos=waiter.position,
            total=total,
            eta=_fmt_eta(eta_secs),
        )
        try:
            await waiter.update_ui(text, None)
        except Exception as exc:
            logger.debug("Queue position update failed: %s", exc)

    @property
    def queue_depth(self) -> int:
        """Number of tasks currently waiting (not including active ones)."""
        return len(self._waiters)
