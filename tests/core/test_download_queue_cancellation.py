import asyncio

import pytest

from app.core.download_queue import DownloadQueue, QueueUnavailable


async def _noop_ui(_text: str, _markup: object | None) -> None:
    return None


async def _wait_for_depth(queue: DownloadQueue, depth: int) -> None:
    async with asyncio.timeout(1):
        while queue.queue_depth != depth:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_waiter_removed_before_grant_and_slot_is_reusable():
    """Catches a cancelled queued request consuming the next released slot."""
    queue = DownloadQueue(asyncio.BoundedSemaphore(1), timeout_seconds=1)
    active = await queue.acquire(_noop_ui)

    async def acquire_waiter():
        return await queue.acquire(_noop_ui)

    waiter = asyncio.create_task(acquire_waiter())
    await _wait_for_depth(queue, 1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await active.release()

    async with asyncio.timeout(1):
        async with queue.acquire(_noop_ui):
            pass
    assert queue.queue_depth == 0


@pytest.mark.asyncio
async def test_grant_cancel_race_never_loses_or_duplicates_slot():
    """Catches non-atomic grant/cancel handling under either race outcome."""
    queue = DownloadQueue(asyncio.BoundedSemaphore(1), timeout_seconds=1)

    for iteration in range(50):
        active = await queue.acquire(_noop_ui)

        async def wait_until_cancelled() -> None:
            lease = await queue.acquire(_noop_ui)
            try:
                await asyncio.Future()
            finally:
                await lease.release()

        waiter = asyncio.create_task(wait_until_cancelled())
        await _wait_for_depth(queue, 1)

        if iteration % 2:
            release = asyncio.create_task(active.release())
            await asyncio.sleep(0)
            waiter.cancel()
        else:
            waiter.cancel()
            release = asyncio.create_task(active.release())
        await asyncio.gather(waiter, release, return_exceptions=True)

        async with asyncio.timeout(1):
            lease = await queue.acquire(_noop_ui)
        await lease.release()
        assert queue.queue_depth == 0


@pytest.mark.asyncio
async def test_lease_release_is_idempotent_without_semaphore_private_state():
    """Catches double release and reliance on ``Semaphore._value``."""

    class PublicSemaphoreOnly:
        def __init__(self) -> None:
            self._semaphore = asyncio.BoundedSemaphore(1)

        def __getattribute__(self, name: str):
            if name == "_value":
                raise AssertionError("DownloadQueue must not read Semaphore._value")
            return object.__getattribute__(self, name)

        def locked(self) -> bool:
            return self._semaphore.locked()

        async def acquire(self) -> bool:
            return await self._semaphore.acquire()

        def release(self) -> None:
            self._semaphore.release()

    queue = DownloadQueue(PublicSemaphoreOnly(), timeout_seconds=1)  # type: ignore[arg-type]
    lease = await queue.acquire(_noop_ui)
    await lease.release()
    await lease.release()

    async with asyncio.timeout(1):
        async with queue.acquire(_noop_ui):
            pass


@pytest.mark.asyncio
async def test_timeout_grant_race_always_returns_the_slot():
    """Catches a timeout winning after a grant without forwarding the slot."""
    queue = DownloadQueue(asyncio.BoundedSemaphore(1), timeout_seconds=0.002)

    for iteration in range(30):
        active = await queue.acquire(_noop_ui)

        async def wait_for_lease():
            try:
                return await queue.acquire(_noop_ui)
            except QueueUnavailable:
                return None

        waiter = asyncio.create_task(wait_for_lease())
        await _wait_for_depth(queue, 1)

        async def release_near_deadline(held=active, race_iteration=iteration) -> None:
            await asyncio.sleep(0 if race_iteration % 2 else 0.004)
            await held.release()

        lease, _ = await asyncio.gather(waiter, release_near_deadline())
        if lease is not None:
            await lease.release()

        async with asyncio.timeout(1):
            async with queue.acquire(_noop_ui):
                pass
        assert queue.queue_depth == 0
