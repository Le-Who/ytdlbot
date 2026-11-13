"""Bounded candidate resolution, with owned and awaited cancellation."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count

from app.core.metrics import metrics

from .models import MediaCandidate, MediaRequest
from .registry import FailureKind, ProviderError, ProviderRoute
from .validation import CandidateValidationResult

Sleeper = Callable[[float], Awaitable[None]]
Validator = Callable[[MediaRequest, MediaCandidate], CandidateValidationResult]
_RETRY_DELAYS = (0.5, 1.0)


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
    alternatives: tuple[MediaCandidate, ...] = ()


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
    attempted_providers: tuple[str, ...] = ()


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

    Two cheap workers try fresh routes before bounded transient retries. A single
    delayed heavy worker shares the remaining budget across local routes.
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
    cheap = [
        (started, index, route, 0, "")
        for index, route in enumerate(eligible)
        if not route.provider.is_heavy
    ]
    retry_order = count(len(eligible))
    heavy = [route for route in eligible if route.provider.is_heavy]
    cheap_count = min(2, sum(not route.provider.is_heavy for route in eligible))
    pending = cheap_count + bool(heavy)
    finished: asyncio.Future[CandidateWinner | None] = (
        asyncio.get_running_loop().create_future()
    )
    failures: list[ProviderFailure] = []
    rejections: list[CandidateRejection] = []
    attempted_providers: list[str] = []
    route_positions = {id(route): index for index, route in enumerate(eligible)}
    validated: list[tuple[int, int, CandidateWinner]] = []

    async def attempt_route(
        route: ProviderRoute,
        attempt_number: int = 0,
        retry_reason: str = "",
        timeout: float | None = None,
    ) -> tuple[float, str] | None:
        """Run one admitted resolve and return a bounded retry delay, if useful."""
        provider = route.provider.name
        if not finished.done() and clock() < deadline:
            if not route.is_available(request):
                return None
            permit = route.breaker.try_acquire(provider, request.platform)
            if permit is None:
                return None
            if attempt_number:
                metrics.retries.inc(
                    reason=retry_reason,
                    provider=provider,
                    platform=request.platform,
                )
            if provider not in attempted_providers:
                attempted_providers.append(provider)
            try:
                candidates = await _resolve(
                    route,
                    request,
                    min(
                        config.resolve_timeout,
                        deadline - clock(),
                        timeout if timeout is not None else config.resolve_timeout,
                    ),
                    sleep,
                )
            except ProviderError as error:
                route.breaker.record_failure(
                    provider, request.platform, error, permit=permit
                )
                failures.append(
                    ProviderFailure(provider, error.kind, str(error), error.retry_after)
                )
                if (
                    not route.provider.is_heavy
                    and error.kind is FailureKind.TRANSIENT
                    and attempt_number < len(_RETRY_DELAYS)
                    and not finished.done()
                    and route.is_available(request)
                ):
                    delay = error.retry_after
                    if delay is not None and (not math.isfinite(delay) or delay < 0):
                        return None
                    delay = max(_RETRY_DELAYS[attempt_number], delay or 0)
                    if delay < deadline - clock():
                        return (
                            delay,
                            "provider_retry_after"
                            if error.retry_after is not None
                            else "provider_transient",
                        )
                return None
            except asyncio.CancelledError:
                route.breaker.release(provider, request.platform, permit=permit)
                raise
            else:
                route.breaker.record_success(provider, request.platform, permit=permit)
                usable: list[MediaCandidate] = []
                for candidate in candidates:
                    validation = validate(request, candidate)
                    if validation.usable:
                        usable.append(candidate)
                    else:
                        rejections.append(
                            CandidateRejection(provider, candidate, validation)
                        )
                if usable:
                    for candidate_index, candidate in enumerate(usable):
                        validated.append(
                            (
                                route_positions[id(route)],
                                candidate_index,
                                CandidateWinner(provider, candidate),
                            )
                        )
                    if not finished.done() and clock() < deadline:
                        finished.set_result(
                            CandidateWinner(provider, usable[0], tuple(usable[1:]))
                        )
                    return None
        return None

    async def worker(*, is_heavy: bool = False) -> None:
        nonlocal pending
        try:
            if is_heavy:
                await sleep(max(0, started + config.heavy_delay - clock()))
                for index, route in enumerate(heavy):
                    # A slow first extractor must leave a usable window for its
                    # sequential fallback without extending the request deadline.
                    await attempt_route(
                        route, timeout=(deadline - clock()) / (len(heavy) - index)
                    )
                    if finished.done():
                        break
            else:
                while cheap:
                    if finished.done() or clock() >= deadline:
                        break
                    ready_at, _, route, attempt_number, retry_reason = heappop(cheap)
                    if ready_at > clock():
                        await sleep(ready_at - clock())
                    retry = await attempt_route(route, attempt_number, retry_reason)
                    if retry is not None:
                        delay, reason = retry
                        heappush(
                            cheap,
                            (
                                clock() + delay,
                                next(retry_order),
                                route,
                                attempt_number + 1,
                                reason,
                            ),
                        )
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
    if winner is not None:
        ordered = [entry[2] for entry in sorted(validated, key=lambda entry: entry[:2])]
        alternatives = tuple(
            item.candidate
            for item in ordered
            if not (
                item.provider == winner.provider and item.candidate is winner.candidate
            )
        )
        winner = CandidateWinner(winner.provider, winner.candidate, alternatives)
        metrics.provider_wins.inc(
            provider=winner.provider,
            platform=request.platform,
        )
    return RaceResult(
        winner,
        tuple(failures),
        tuple(rejections),
        tuple(attempted_providers),
    )
