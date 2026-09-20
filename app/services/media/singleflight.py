"""Cancellation-safe in-process coalescing for identical async work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
T = TypeVar("T")


@dataclass(slots=True)
class _FactoryMarker:
    group_id: int
    key: Hashable
    active: bool = True


_ACTIVE_FACTORIES: ContextVar[tuple[_FactoryMarker, ...]] = ContextVar(
    "singleflight_active_factories", default=()
)


@dataclass(slots=True)
class _Flight(Generic[T]):
    task: asyncio.Task[T]
    subscribers: int = 0


class SingleFlightReentryError(RuntimeError):
    """A shared factory tried to subscribe to its own group/key flight."""


class SingleFlightGroup(Generic[K, T]):
    """Share one task per key without sharing subscriber cancellation."""

    def __init__(self) -> None:
        self._flights: dict[K, _Flight[T]] = {}
        self._lock = asyncio.Lock()

    @property
    def inflight_count(self) -> int:
        return len(self._flights)

    @property
    def subscriber_count(self) -> int:
        return sum(flight.subscribers for flight in self._flights.values())

    async def do(self, key: K, work: Callable[[], Awaitable[T]]) -> T:
        inherited = _ACTIVE_FACTORIES.get()
        active = tuple(marker for marker in inherited if marker.active)
        if len(active) != len(inherited):
            _ACTIVE_FACTORIES.set(active)
        if any(marker.group_id == id(self) and marker.key == key for marker in active):
            raise SingleFlightReentryError(
                "reentrant singleflight call for the active group and key"
            )
        async with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                marker = _FactoryMarker(id(self), key)
                flight = _Flight(asyncio.create_task(_invoke(work, marker)))
                self._flights[key] = flight
            elif flight.task is asyncio.current_task():
                raise SingleFlightReentryError(
                    "reentrant singleflight call for the active group and key"
                )
            flight.subscribers += 1

        try:
            return await asyncio.shield(flight.task)
        finally:
            await self._unsubscribe_uninterruptibly(key, flight)

    async def _unsubscribe_uninterruptibly(self, key: K, flight: _Flight[T]) -> None:
        cleanup = asyncio.create_task(self._unsubscribe(key, flight))
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
        if cancellation is not None:
            if not cleanup.cancelled():
                cleanup.exception()
            raise cancellation
        await cleanup

    async def _unsubscribe(self, key: K, flight: _Flight[T]) -> None:
        cleanup: asyncio.Task[T] | None = None
        async with self._lock:
            current = self._flights.get(key)
            if current is not flight:
                return
            flight.subscribers -= 1
            if flight.subscribers == 0:
                self._flights.pop(key, None)
                if not flight.task.done():
                    flight.task.cancel()
                    cleanup = flight.task
                else:
                    # Retrieve an exception even if a subscriber was cancelled
                    # exactly as the shared task finished.
                    if not flight.task.cancelled():
                        flight.task.exception()
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)


async def _invoke(work: Callable[[], Awaitable[T]], marker: _FactoryMarker) -> T:
    inherited = tuple(item for item in _ACTIVE_FACTORIES.get() if item.active)
    token = _ACTIVE_FACTORIES.set((*inherited, marker))
    try:
        return await work()
    finally:
        marker.active = False
        _ACTIVE_FACTORIES.reset(token)
