import asyncio
from dataclasses import dataclass, field, replace

import pytest

from app.services.media.models import MediaCandidate, MediaRequest
from app.services.media.race import RaceConfig, race_candidates
from app.services.media.registry import (
    CircuitBreaker,
    FailureKind,
    ProviderError,
    ProviderRoute,
)
from app.services.media.validation import CandidateRejectionReason, validate_candidate


async def settle():
    for _ in range(40):
        await asyncio.sleep(0)


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.sleepers = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        entry = (self.now + seconds, future)
        self.sleepers.append(entry)
        try:
            await future
        finally:
            self.sleepers.remove(entry)

    async def advance(self, seconds):
        await settle()
        self.now += seconds
        for when, future in list(self.sleepers):
            if when <= self.now and not future.done():
                future.set_result(None)
        await settle()


GOOD = MediaCandidate("good", "https://cdn.example/video", audio_languages=("uk",))
WRONG_AUDIO = replace(GOOD, candidate_id="wrong", audio_languages=("en",))
REQUEST = MediaRequest.from_url("https://youtu.be/abc", audio_language="uk")


@dataclass
class Provider:
    name: str
    clock: ManualClock
    delay: float = 0
    candidates: list[MediaCandidate] = field(default_factory=lambda: [GOOD])
    error: Exception | None = None
    is_heavy: bool = False
    supported: bool = True
    backend_family: str = "test"
    starts: list[float] = field(default_factory=list)
    cancelled: bool = False
    finished: bool = False

    def supports(self, request):
        return self.supported

    async def resolve(self, request):
        assert request == REQUEST or request.canonical_url == REQUEST.canonical_url
        self.starts.append(self.clock.now)
        try:
            if self.delay:
                await self.clock.sleep(self.delay)
            if self.error:
                error, self.error = self.error, None
                raise error
            return self.candidates
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0)
            raise
        finally:
            self.finished = True


def start(clock, providers, request=REQUEST, **config):
    routes = [
        p if isinstance(p, ProviderRoute) else ProviderRoute(p) for p in providers
    ]
    return asyncio.create_task(
        race_candidates(
            request,
            routes,
            validate_candidate,
            config=RaceConfig(**config),
            clock=clock,
            sleep=clock.sleep,
        )
    )


async def result_of(task):
    return await asyncio.wait_for(task, timeout=0.5)


async def test_second_valid_provider_wins_without_waiting_for_hung_first():
    clock = ManualClock()
    first, second = Provider("first", clock, 15), Provider("second", clock, 1)
    task = start(clock, [first, second])
    await clock.advance(1)
    result = await result_of(task)
    assert result.winner.provider == "second"
    assert result.winner.candidate == GOOD
    assert clock.now < 2
    assert first.cancelled and first.finished
    assert clock.sleepers == []


@pytest.mark.parametrize("exact", [False, True])
async def test_fast_invalid_candidate_never_beats_slower_equivalent_candidate(exact):
    clock = ManualClock()
    task = start(
        clock,
        [Provider("bad", clock, 0.1, [WRONG_AUDIO]), Provider("good", clock, 0.2)],
        request=replace(REQUEST, exact=exact),
    )
    await clock.advance(0.1)
    assert not task.done()
    await clock.advance(0.1)
    result = await result_of(task)
    assert result.winner.provider == "good"
    assert result.rejections[0].validation.reasons == (
        CandidateRejectionReason.AUDIO_LANGUAGE_UNAVAILABLE,
    )


async def test_later_candidate_from_same_provider_can_win():
    clock = ManualClock()
    result = await result_of(
        start(clock, [Provider("multi", clock, candidates=[WRONG_AUDIO, GOOD])])
    )
    assert result.winner.candidate.candidate_id == "good"
    assert len(result.rejections) == 1


async def test_only_two_cheap_resolves_run_and_next_starts_when_slot_frees():
    clock = ManualClock()
    providers = [
        Provider("first", clock, 1, []),
        Provider("second", clock, 15),
        Provider("third", clock),
    ]
    task = start(clock, providers)
    await settle()
    assert [len(p.starts) for p in providers] == [1, 1, 0]
    await clock.advance(1)
    result = await result_of(task)
    assert result.winner.provider == "third"
    assert providers[1].cancelled


async def test_heavy_fallback_starts_after_1_5_seconds_and_only_one_heavy_runs():
    clock = ManualClock()
    cheap = [Provider("first", clock, 15), Provider("second", clock, 15)]
    heavy = [
        Provider("local", clock, is_heavy=True),
        Provider("other-local", clock, is_heavy=True),
    ]
    task = start(clock, cheap + heavy)
    await clock.advance(1.49)
    assert heavy[0].starts == []
    await clock.advance(0.01)
    result = await result_of(task)
    assert result.winner.provider == "local"
    assert heavy[0].starts == [1.5]
    assert heavy[1].starts == []
    assert all(p.cancelled for p in cheap)
    assert clock.sleepers == []


async def test_cheap_winner_cancels_heavy_delay():
    clock = ManualClock()
    heavy = Provider("local", clock, is_heavy=True)
    result = await result_of(start(clock, [Provider("cheap", clock), heavy]))
    assert result.winner.provider == "cheap"
    assert heavy.starts == []
    assert clock.sleepers == []


async def test_filtered_routes_add_no_wait_or_resolve_tasks():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure("open", "youtube", ProviderError(FailureKind.TRANSIENT))
    providers = [
        Provider("disabled", clock, 15),
        Provider("unsupported", clock, 15, supported=False),
        Provider("unavailable", clock, 15),
        Provider("open", clock, 15),
    ]
    routes = [
        ProviderRoute(providers[0], enabled=False),
        ProviderRoute(providers[1]),
        ProviderRoute(providers[2], capability_available=False),
        ProviderRoute(providers[3], breaker=breaker),
    ]
    result = await result_of(start(clock, routes))
    assert result.winner is None
    assert all(not p.starts for p in providers)
    assert clock.sleepers == []


async def test_retry_after_longer_than_remaining_budget_does_not_wait_or_retry():
    clock = ManualClock()
    provider = Provider(
        "limited", clock, error=ProviderError(FailureKind.TRANSIENT, retry_after=21)
    )
    result = await result_of(start(clock, [provider]))
    assert result.winner is None
    assert provider.starts == [0]
    assert result.failures[0].retry_after == 21
    assert clock.sleepers == []


async def test_retry_after_within_budget_waits_before_retry():
    clock = ManualClock()
    provider = Provider(
        "limited", clock, error=ProviderError(FailureKind.TRANSIENT, retry_after=2)
    )
    task = start(clock, [provider])
    await clock.advance(1)
    assert provider.starts == [0]
    await clock.advance(1)
    result = await result_of(task)
    assert result.winner.provider == "limited"
    assert provider.starts == [0, 2]


async def test_resolve_timeout_at_eight_seconds_releases_slot_for_next_route():
    clock = ManualClock()
    first, second, third = (
        Provider("first", clock, 15),
        Provider("second", clock, 15),
        Provider("third", clock),
    )
    task = start(clock, [first, second, third])
    await clock.advance(7.99)
    assert third.starts == []
    await clock.advance(0.01)
    result = await result_of(task)
    assert result.winner.provider == "third"
    assert first.cancelled and second.cancelled
    assert any(f.kind is FailureKind.TRANSIENT for f in result.failures)


async def test_total_budget_ends_all_attempts_at_twenty_seconds():
    clock = ManualClock()
    providers = [Provider(str(i), clock, 30) for i in range(8)]
    task = start(clock, providers)
    await clock.advance(8)
    await clock.advance(8)
    await clock.advance(4)
    result = await result_of(task)
    assert result.winner is None
    assert [p.starts for p in providers] == [[0], [0], [8], [8], [16], [16], [], []]
    assert all(p.finished for p in providers[:6])
    assert clock.sleepers == []


async def test_request_deadline_shortens_total_budget():
    clock = ManualClock()
    provider = Provider("slow", clock, 15)
    task = start(clock, [provider], request=replace(REQUEST, deadline=1))
    await clock.advance(1)
    assert (await result_of(task)).winner is None
    assert provider.cancelled


async def test_caller_cancellation_awaits_provider_cleanup_and_delay_tasks():
    clock = ManualClock()
    slow, heavy = Provider("slow", clock, 15), Provider("heavy", clock, is_heavy=True)
    task = start(clock, [slow, heavy])
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result_of(task)
    assert slow.cancelled and slow.finished
    assert heavy.starts == []
    assert clock.sleepers == []


async def test_untyped_error_message_is_not_classified_as_auth_or_transient():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    provider = Provider(
        "bug", clock, error=RuntimeError("unauthorized authentication timeout")
    )
    result = await result_of(start(clock, [ProviderRoute(provider, breaker=breaker)]))
    assert result.winner is None
    assert result.failures[0].kind is FailureKind.INTERNAL
    assert breaker.is_available("bug", "youtube")


async def test_typed_auth_failure_disables_shared_route_in_later_races():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    provider = Provider("auth", clock, error=ProviderError(FailureKind.AUTH))
    route = ProviderRoute(provider, breaker=breaker)
    await result_of(start(clock, [route]))
    assert (await result_of(start(clock, [route]))).winner is None
    assert provider.starts == [0]


async def test_concurrent_races_admit_only_one_half_open_probe():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    provider = Provider("probe", clock, 1)
    for _ in range(3):
        breaker.record_failure("probe", "youtube", ProviderError(FailureKind.TRANSIENT))
    await clock.advance(300)
    route = ProviderRoute(provider, breaker=breaker)
    first, second = start(clock, [route]), start(clock, [route])
    await settle()
    assert provider.starts == [300]
    assert (await result_of(second)).winner is None
    await clock.advance(1)
    assert (await result_of(first)).winner.provider == "probe"
    assert breaker.is_available("probe", "youtube")


async def test_cancelled_half_open_race_releases_probe_for_next_request():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    provider = Provider("probe", clock, 15)
    for _ in range(3):
        breaker.record_failure("probe", "youtube", ProviderError(FailureKind.TRANSIENT))
    await clock.advance(300)
    task = start(clock, [ProviderRoute(provider, breaker=breaker)])
    await settle()
    assert not breaker.is_available("probe", "youtube")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result_of(task)
    assert breaker.try_acquire("probe", "youtube")
    assert provider.cancelled and provider.finished
    assert clock.sleepers == []


async def test_queued_route_is_rechecked_before_admission():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)
    queued = Provider("queued", clock)
    task = start(
        clock,
        [
            Provider("first", clock, 1, []),
            Provider("second", clock, 1, []),
            ProviderRoute(queued, breaker=breaker),
        ],
    )
    await settle()
    breaker.record_failure("queued", "youtube", ProviderError(FailureKind.CONFIG))
    await clock.advance(1)
    assert (await result_of(task)).winner is None
    assert queued.starts == []


async def test_failed_heavy_route_does_not_start_second_heavy_provider():
    clock = ManualClock()
    first = Provider("local-one", clock, candidates=[], is_heavy=True)
    second = Provider("local-two", clock, is_heavy=True)
    task = start(clock, [first, second])
    await clock.advance(1.5)
    assert (await result_of(task)).winner is None
    assert first.starts == [1.5]
    assert second.starts == []
    assert clock.sleepers == []


async def test_retry_loop_stops_immediately_when_third_failure_opens_breaker():
    clock = ManualClock()
    breaker = CircuitBreaker(clock=clock)

    class RateLimitedProvider(Provider):
        async def resolve(self, request):
            self.error = ProviderError(FailureKind.TRANSIENT, retry_after=1)
            return await super().resolve(request)

    provider = RateLimitedProvider("limited", clock)
    task = start(clock, [ProviderRoute(provider, breaker=breaker)])
    await clock.advance(1)
    await clock.advance(1)
    result = await result_of(task)
    assert result.winner is None
    assert len(result.failures) == 3
    assert provider.starts == [0, 1, 2]
    assert not breaker.is_available("limited", "youtube")
    assert clock.sleepers == []
