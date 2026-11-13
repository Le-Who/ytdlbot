import asyncio

import pytest

from app.services.media.models import MediaRequest
from app.services.media.race import RaceConfig, race_candidates
from app.services.media.registry import (
    CircuitBreaker,
    FailureKind,
    ProviderError,
    ProviderRoute,
)
from app.services.media.validation import validate_candidate


@pytest.mark.parametrize("old_outcome", ["success", "cancel", "permanent", "transient"])
async def test_old_resolve_cannot_complete_a_newer_half_open_probe(old_outcome):
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    started = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    class Provider:
        name = "shared"
        backend_family = "test"
        is_heavy = False
        calls = 0

        def supports(self, request):
            return True

        async def resolve(self, request):
            index = self.calls
            self.calls += 1
            started[index].set()
            await finish[index].wait()
            if index == 0 and old_outcome in {"permanent", "transient"}:
                raise ProviderError(FailureKind(old_outcome))
            return []

    provider = Provider()
    route = ProviderRoute(provider, breaker=breaker)
    request = MediaRequest.from_url("https://youtu.be/abc")

    async def resolve():
        return await race_candidates(
            request,
            [route],
            validate_candidate,
            config=RaceConfig(resolve_timeout=1000, total_timeout=2000),
            clock=lambda: now[0],
        )

    old = asyncio.create_task(resolve())
    probe = None
    try:
        await asyncio.wait_for(started[0].wait(), 1)
        for _ in range(3):
            breaker.record_failure(
                "shared", "youtube", ProviderError(FailureKind.TRANSIENT)
            )
        now[0] = 300
        probe = asyncio.create_task(resolve())
        await asyncio.wait_for(started[1].wait(), 1)

        if old_outcome == "cancel":
            old.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old
        else:
            finish[0].set()
            await asyncio.wait_for(old, 1)

        assert not breaker.is_available("shared", "youtube"), (
            "the newer probe must retain exclusive admission"
        )
        finish[1].set()
        await asyncio.wait_for(probe, 1)
        assert breaker.is_available("shared", "youtube"), (
            "only the newer probe's outcome may close its circuit"
        )
        assert provider.calls == 2
    finally:
        old.cancel()
        if probe is not None:
            probe.cancel()
        await asyncio.gather(
            old, *([probe] if probe is not None else []), return_exceptions=True
        )
