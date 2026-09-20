"""Provider routing and per-provider/platform circuit health.

The registry and breaker are owned by one asyncio event loop. Admission and
health updates are synchronous so half-open admission is atomic within it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from .models import MediaCandidate, MediaRequest


class MediaProvider(Protocol):
    name: str
    backend_family: str
    is_heavy: bool

    def supports(self, request: MediaRequest) -> bool: ...

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]: ...


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    AUTH = "auth"
    CONFIG = "config"
    PERMANENT = "permanent"
    INTERNAL = "internal"


class ProviderError(Exception):
    """An adapter-classified failure; Retry-After is a delay in seconds."""

    def __init__(
        self, kind: FailureKind, message: str = "", *, retry_after: float | None = None
    ) -> None:
        super().__init__(message or kind.value)
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class _Circuit:
    failures: deque[float] = field(default_factory=deque)
    open_until: float | None = None
    probe_inflight: bool = False
    disabled: bool = False


class CircuitBreaker:
    """Three transient failures in 60 s open a route for 300 s."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._circuits: dict[tuple[str, str], _Circuit] = {}

    def _state(self, provider: str, platform: str) -> _Circuit:
        return self._circuits.setdefault((provider, platform), _Circuit())

    def is_available(self, provider: str, platform: str) -> bool:
        state = self._state(provider, platform)
        return (
            not state.disabled
            and not state.probe_inflight
            and (state.open_until is None or self._clock() >= state.open_until)
        )

    def try_acquire(self, provider: str, platform: str) -> bool:
        if not self.is_available(provider, platform):
            return False
        state = self._state(provider, platform)
        if state.open_until is not None:
            state.probe_inflight = True
        return True

    def release(self, provider: str, platform: str) -> None:
        """Release a cancelled probe without declaring the route healthy."""
        self._state(provider, platform).probe_inflight = False

    def record_success(self, provider: str, platform: str) -> None:
        state = self._state(provider, platform)
        if state.probe_inflight:
            state.open_until = None
            state.probe_inflight = False
            state.failures.clear()

    def record_failure(
        self, provider: str, platform: str, error: ProviderError
    ) -> None:
        state = self._state(provider, platform)
        was_probe = state.probe_inflight
        state.probe_inflight = False
        if error.kind in (FailureKind.AUTH, FailureKind.CONFIG):
            state.disabled = True
        elif error.kind is FailureKind.TRANSIENT:
            now = self._clock()
            while state.failures and state.failures[0] < now - 60:
                state.failures.popleft()
            state.failures.append(now)
            if was_probe or len(state.failures) >= 3:
                state.open_until = now + 300

    def reset_configuration(self) -> None:
        """A registry revision permits routes disabled by auth/config to retry."""
        for state in self._circuits.values():
            state.disabled = False


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    provider: MediaProvider
    enabled: bool = True
    capability_available: bool = True
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    def is_available(self, request: MediaRequest) -> bool:
        return (
            self.enabled
            and self.capability_available
            and self.provider.supports(request)
            and self.breaker.is_available(self.provider.name, request.platform)
        )


class ProviderRegistry:
    def __init__(
        self,
        routes: Iterable[ProviderRoute] = (),
        *,
        breaker: CircuitBreaker | None = None,
        revision: str = "initial",
    ) -> None:
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self._routes = tuple(replace(route, breaker=self.breaker) for route in routes)
        self._revision = revision

    def set_revision(self, revision: str) -> None:
        if revision != self._revision:
            self._revision = revision
            self.breaker.reset_configuration()

    def routes_for(self, request: MediaRequest) -> tuple[ProviderRoute, ...]:
        """Inspect eligibility without reserving a half-open probe."""
        return tuple(route for route in self._routes if route.is_available(request))
