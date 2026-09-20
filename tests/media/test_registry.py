from dataclasses import dataclass

import pytest

from app.services.media.models import MediaCandidate, MediaRequest
from app.services.media.registry import (
    CircuitBreaker,
    FailureKind,
    ProviderError,
    ProviderRegistry,
    ProviderRoute,
)


@dataclass
class StubProvider:
    name: str = "remote"
    backend_family: str = "test"
    is_heavy: bool = False
    supported: bool = True

    def supports(self, request: MediaRequest) -> bool:
        return self.supported

    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        raise AssertionError("registry must not call providers")


def test_three_failures_in_window_open_only_provider_platform_key():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    error = ProviderError(FailureKind.TRANSIENT)
    for moment in (0, 20, 59):
        now[0] = moment
        assert breaker.try_acquire("remote", "youtube")
        breaker.record_failure("remote", "youtube", error)

    assert not breaker.is_available("remote", "youtube")
    assert breaker.is_available("remote", "other")
    assert breaker.is_available("another", "youtube")
    now[0] = 358.99
    assert not breaker.try_acquire("remote", "youtube")
    now[0] = 359
    assert breaker.try_acquire("remote", "youtube")
    assert not breaker.try_acquire("remote", "youtube")
    breaker.record_success("remote", "youtube")
    assert breaker.try_acquire("remote", "youtube")
    assert breaker.try_acquire("remote", "youtube")


def test_expired_transient_failures_do_not_open_circuit():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    for moment in (0, 61, 62):
        now[0] = moment
        breaker.record_failure(
            "remote", "youtube", ProviderError(FailureKind.TRANSIENT)
        )
    assert breaker.is_available("remote", "youtube")


def test_failed_half_open_probe_reopens_and_cancelled_probe_releases_slot():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    error = ProviderError(FailureKind.TRANSIENT)
    for _ in range(3):
        breaker.record_failure("remote", "youtube", error)
    now[0] = 300
    assert breaker.try_acquire("remote", "youtube")
    breaker.release("remote", "youtube")
    assert breaker.try_acquire("remote", "youtube")
    breaker.record_failure("remote", "youtube", error)
    now[0] = 599
    assert not breaker.is_available("remote", "youtube")
    now[0] = 600
    assert breaker.try_acquire("remote", "youtube")


@pytest.mark.parametrize("kind", [FailureKind.AUTH, FailureKind.CONFIG])
def test_auth_config_remains_disabled_until_registry_revision_changes(kind):
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    registry = ProviderRegistry(
        [ProviderRoute(StubProvider())], breaker=breaker, revision="v1"
    )
    request = MediaRequest.from_url("https://youtu.be/abc")
    breaker.record_failure("remote", "youtube", ProviderError(kind))
    now[0] = 10000
    assert registry.routes_for(request) == ()
    registry.set_revision("v1")
    assert registry.routes_for(request) == ()
    registry.set_revision("v2")
    assert len(registry.routes_for(request)) == 1


def test_registry_filters_disabled_unsupported_unavailable_and_open_routes():
    breaker = CircuitBreaker()
    registry = ProviderRegistry(
        [
            ProviderRoute(StubProvider("disabled"), enabled=False),
            ProviderRoute(StubProvider("unsupported", supported=False)),
            ProviderRoute(StubProvider("unavailable"), capability_available=False),
            ProviderRoute(StubProvider("open")),
            ProviderRoute(StubProvider("healthy")),
        ],
        breaker=breaker,
    )
    for _ in range(3):
        breaker.record_failure("open", "youtube", ProviderError(FailureKind.TRANSIENT))
    routes = registry.routes_for(MediaRequest.from_url("https://youtu.be/abc"))
    assert [route.provider.name for route in routes] == ["healthy"]
    assert routes[0].breaker is breaker


def test_permanent_failure_does_not_trip_or_disable_circuit():
    breaker = CircuitBreaker()
    for _ in range(4):
        breaker.record_failure(
            "remote", "youtube", ProviderError(FailureKind.PERMANENT)
        )
    assert breaker.is_available("remote", "youtube")
