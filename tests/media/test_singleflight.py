from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from app.services.media.singleflight import SingleFlightGroup


@pytest.mark.asyncio
async def test_cancelling_one_subscriber_keeps_shared_work_for_other_subscriber():
    """Catches subscriber cancellation propagating into shared materialization."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def materialize() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "media-path"

    first = asyncio.create_task(group.do("same", materialize))
    second = asyncio.create_task(group.do("same", materialize))
    await started.wait()
    while group.subscriber_count < 2:
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    assert await second == "media-path"
    assert calls == 1
    assert group.inflight_count == 0


@pytest.mark.asyncio
async def test_last_cancelled_subscriber_cleans_up_shared_work():
    """Catches abandoned work and inflight entries surviving the last subscriber."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def materialize() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "unreachable"

    subscriber = asyncio.create_task(group.do("same", materialize))
    await started.wait()
    subscriber.cancel()

    with pytest.raises(asyncio.CancelledError):
        await subscriber
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert group.inflight_count == 0
    assert group.subscriber_count == 0


@pytest.mark.asyncio
async def test_shared_exception_reaches_every_subscriber_and_does_not_leak_entry():
    """Catches a failed resolve being swallowed or poisoning the next request."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    release = asyncio.Event()
    calls = 0

    async def resolve() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        raise RuntimeError("provider failed")

    first = asyncio.create_task(group.do("same", resolve))
    second = asyncio.create_task(group.do("same", resolve))
    while group.subscriber_count < 2:
        await asyncio.sleep(0)
    release.set()

    for subscriber in (first, second):
        with pytest.raises(RuntimeError, match="provider failed"):
            await subscriber
    assert calls == 1
    assert group.inflight_count == 0

    async def recover() -> str:
        return "fresh"

    assert await group.do("same", recover) == "fresh"


@pytest.mark.asyncio
async def test_distinct_keys_do_not_share_resolve_work():
    """Catches unrelated request keys receiving another request's result."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    release = asyncio.Event()
    calls: list[str] = []

    async def work(value: str) -> str:
        calls.append(value)
        await release.wait()
        return value

    first = asyncio.create_task(group.do("one", lambda: work("first")))
    second = asyncio.create_task(group.do("two", lambda: work("second")))
    while group.subscriber_count < 2:
        await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == ["first", "second"]
    assert sorted(calls) == ["first", "second"]
    assert group.inflight_count == 0


@pytest.mark.asyncio
async def test_same_group_same_key_factory_reentry_fails_fast_and_recovers():
    """Catches a shared factory awaiting itself and recursing during cancellation."""
    script = textwrap.dedent(
        """
        import asyncio
        from app.services.media.singleflight import SingleFlightGroup

        async def main():
            group = SingleFlightGroup()

            async def reenter():
                return await asyncio.create_task(
                    group.do(
                        "same", lambda: asyncio.sleep(0, result="unreachable")
                    )
                )

            try:
                await group.do("same", reenter)
            except RuntimeError as error:
                assert "reentrant singleflight" in str(error).lower()
            else:
                raise AssertionError("same-key reentry was accepted")

            assert group.inflight_count == 0
            assert group.subscriber_count == 0
            assert await group.do(
                "same", lambda: asyncio.sleep(0, result="fresh")
            ) == "fresh"

        asyncio.run(main())
        """
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()

    assert process.returncode == 0, (stdout + stderr).decode(errors="replace")


@pytest.mark.asyncio
async def test_nested_different_key_and_different_group_are_allowed():
    """Catches an over-broad reentry guard that rejects independent work."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    other: SingleFlightGroup[str, str] = SingleFlightGroup()

    async def nested() -> str:
        different_key = await group.do(
            "different", lambda: asyncio.sleep(0, result="key")
        )
        different_group = await other.do(
            "same", lambda: asyncio.sleep(0, result="group")
        )
        return f"{different_key}-{different_group}"

    assert await group.do("same", nested) == "key-group"
    assert group.inflight_count == other.inflight_count == 0


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_unsubscribe_cleanup():
    """Catches repeated cancel leaving a subscriber, flight, and factory behind."""
    group: SingleFlightGroup[str, str] = SingleFlightGroup()
    started = asyncio.Event()
    release = asyncio.Event()
    factory_stopped = asyncio.Event()

    async def work() -> str:
        started.set()
        try:
            await release.wait()
        finally:
            factory_stopped.set()
        return "unreachable"

    subscriber = asyncio.create_task(group.do("same", work))
    await started.wait()
    await group._lock.acquire()  # type: ignore[attr-defined]
    try:
        subscriber.cancel()
        await asyncio.sleep(0)
        subscriber.cancel()
        subscriber.cancel()
    finally:
        group._lock.release()  # type: ignore[attr-defined]

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(subscriber), timeout=1)
        await asyncio.wait_for(factory_stopped.wait(), timeout=1)
        assert group.inflight_count == 0
        assert group.subscriber_count == 0
    finally:
        release.set()
        await asyncio.sleep(0)
