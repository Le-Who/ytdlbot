"""Bounded candidate resolution, with owned and awaited cancellation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .models import MediaCandidate, MediaRequest
from .registry import FailureKind, ProviderError, ProviderRoute
from .validation import CandidateValidationResult

Sleeper = Callable[[float], Awaitable[None]]
Validator = Callable[[MediaRequest, MediaCandidate], CandidateValidationResult]


@dataclass(frozen=True, slots=True)
class RaceConfig:
    resolve_timeout: float = 8.0
    total_timeout: float = 20.0
    connect_timeout: float = 3.0
    heavy_delay: float = 1.5


@dataclass(frozen=True, slots=True)
class CandidateWinner:
    provider: str
    candidate: MediaCandidate


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    provider: str
    candidate: MediaCandidate
    validation: CandidateValidationResult


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    provider: str
    kind: FailureKind
    message: str
    retry_after: float | None = None


@dataclass(frozen=True, slots=True)
class RaceResult:
    winner: CandidateWinner | None
    failures: tuple[ProviderFailure, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()


async def _resolve(
    route: ProviderRoute, request: MediaRequest, timeout: float, sleep: Sleeper
) -> list[MediaCandidate]:
    async def resolve_safely() -> list[MediaCandidate] | ProviderError:
        try:
            return await route.provider.resolve(request)
        except ProviderError as error:
            return error
        except Exception as error:  # noqa: BLE001 - isolate unknown adapter bugs as INTERNAL
            # Adapter bugs are isolated, but never guessed to be auth/transient.
            return ProviderError(FailureKind.INTERNAL, str(error))

    async def expire_resolve() -> None:
        await sleep(timeout)

    outcome: list[MediaCandidate] | ProviderError
    async with asyncio.TaskGroup() as group:
        attempt = group.create_task(resolve_safely())
        timer = group.create_task(expire_resolve())
        try:
            done, _ = await asyncio.wait(
                (attempt, timer), return_when=asyncio.FIRST_COMPLETED
            )
            outcome = (
                attempt.result()
                if attempt in done
                else ProviderError(FailureKind.TRANSIENT, "provider resolve timeout")
            )
        finally:
            attempt.cancel()
            timer.cancel()
    if isinstance(outcome, ProviderError):
        raise outcome
    return outcome


async def race_candidates(
    request: MediaRequest,
    routes: Iterable[ProviderRoute],
    validate: Validator,
    *,
    config: RaceConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Sleeper = asyncio.sleep,
) -> RaceResult:
    """Return the first validated candidate, respecting all request constraints.

    Two cheap workers hold their slots through Retry-After waits. A single
    delayed heavy worker invokes local routes sequentially, never concurrently.
    Adapters can consume the same RaceConfig.connect_timeout when they construct
    their HTTP clients.
    """
    if config is None:
        config = RaceConfig()
    started = clock()
    deadline = started + config.total_timeout
    if request.deadline is not None:
        deadline = min(deadline, request.deadline)
    eligible = [route for route in routes if route.is_available(request)]
    if not eligible or deadline <= started:
        return RaceResult(None)
    cheap = iter(route for route in eligible if not route.provider.is_heavy)
    heavy = [route for route in eligible if route.provider.is_heavy]
    cheap_count = min(2, sum(not route.provider.is_heavy for route in eligible))
    pending = cheap_count + bool(heavy)
    finished: asyncio.Future[CandidateWinner | None] = (
        asyncio.get_running_loop().create_future()
    )
    failures: list[ProviderFailure] = []
    rejections: list[CandidateRejection] = []

    async def attempt_route(route: ProviderRoute) -> bool:
        """Return whether this route actually acquired admission and ran."""
        provider = route.provider.name
        ran = False
        while not finished.done() and clock() < deadline:
            if not route.is_available(request) or not route.breaker.try_acquire(
                provider, request.platform
            ):
                return ran
            ran = True
            try:
                candidates = await _resolve(
                    route,
                    request,
                    min(config.resolve_timeout, deadline - clock()),
                    sleep,
                )
            except ProviderError as error:
                route.breaker.record_failure(provider, request.platform, error)
                failures.append(
                    ProviderFailure(provider, error.kind, str(error), error.retry_after)
                )
                delay = error.retry_after
                if (
                    not route.provider.is_heavy
                    and error.kind is FailureKind.TRANSIENT
                    and delay is not None
                    and 0 < delay < deadline - clock()
                    and not finished.done()
                    and route.is_available(request)
                ):
                    await sleep(delay)
                    continue
                return ran
            except asyncio.CancelledError:
                route.breaker.release(provider, request.platform)
                raise
            else:
                route.breaker.record_success(provider, request.platform)
                for candidate in candidates:
                    validation = validate(request, candidate)
                    if validation.usable:
                        if not finished.done() and clock() < deadline:
                            finished.set_result(CandidateWinner(provider, candidate))
                        return ran
                    rejections.append(
                        CandidateRejection(provider, candidate, validation)
                    )
                return ran
        return ran

    async def worker(*, is_heavy: bool = False) -> None:
        nonlocal pending
        try:
            if is_heavy:
                await sleep(max(0, started + config.heavy_delay - clock()))
                for route in heavy:
                    await attempt_route(route)
                    if finished.done():
                        break
            else:
                for route in cheap:
                    if finished.done() or clock() >= deadline:
                        break
                    await attempt_route(route)
        finally:
            pending -= 1
            if not pending and not finished.done():
                finished.set_result(None)

    async def expire() -> None:
        await sleep(max(0, deadline - clock()))
        if not finished.done():
            finished.set_result(None)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(worker()) for _ in range(cheap_count)]
        if heavy:
            tasks.append(group.create_task(worker(is_heavy=True)))
        tasks.append(group.create_task(expire()))
        try:
            winner = await finished
        finally:
            for task in tasks:
                task.cancel()
    return RaceResult(winner, tuple(failures), tuple(rejections))
