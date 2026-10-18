from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import routes
from app.core import state


class HealthyStore:
    async def schema_version(self) -> int:
        return 4

    async def journal_mode(self) -> str:
        return "wal"

    async def busy_timeout(self) -> int:
        return 5_000

    async def write_probe(self) -> None:
        return None


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "_job_store", HealthyStore())
    monkeypatch.setattr(routes, "_local_probe_state", routes._LocalApiProbeState())
    monkeypatch.setattr(routes.config, "TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081")
    monkeypatch.setattr(routes.config, "TELEGRAM_LOCAL_REQUIRED", True)
    monkeypatch.setattr(
        state,
        "bot_app",
        SimpleNamespace(bot=SimpleNamespace(get_me=AsyncMock(return_value=object()))),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


@pytest.mark.asyncio
async def test_readiness_store_failure_cancels_and_awaits_sibling_probes(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "test-private-store-url?token=private-readiness-token"
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    probe_tasks: set[asyncio.Task[object]] = set()
    cancelling = 0
    finished = 0

    class FailingStore(HealthyStore):
        async def schema_version(self) -> int:
            await started.wait()
            raise OSError(secret)

        async def blocked(self) -> None:
            nonlocal cancelling, finished
            task = asyncio.current_task()
            assert task is not None
            probe_tasks.add(task)
            if len(probe_tasks) == 3:
                started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling += 1
                if cancelling == 3:
                    cleanup_started.set()
                await cleanup_release.wait()
                raise
            finally:
                finished += 1

        async def journal_mode(self) -> str:
            await self.blocked()
            return "wal"

        async def busy_timeout(self) -> int:
            await self.blocked()
            return 5_000

        async def write_probe(self) -> None:
            await self.blocked()

    monkeypatch.setattr(routes, "_job_store", FailingStore())
    request = asyncio.create_task(client.get("/health/ready"))
    cleanup_waiter = asyncio.create_task(cleanup_started.wait())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait(
            (request, cleanup_waiter), return_when=asyncio.FIRST_COMPLETED
        )
        assert cleanup_started.is_set(), "readiness returned with live sibling probes"
        assert not request.done(), (
            "readiness did not await sibling cancellation cleanup"
        )
        cleanup_release.set()
        response = await asyncio.wait_for(request, timeout=1)

        assert response.status_code == 503
        assert response.json()["durable_store"] == {"ready": False}
        assert finished == 3
        assert all(task.done() for task in probe_tasks)
        assert secret not in response.text
        assert secret not in repr([vars(record) for record in caplog.records])
    finally:
        cleanup_release.set()
        cleanup_waiter.cancel()
        for task in probe_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(
            request, cleanup_waiter, *probe_tasks, return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["request_cancel", "probe_timeout"])
async def test_readiness_store_probe_settles_all_work_when_stopped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    started = asyncio.Event()
    probe_tasks: set[asyncio.Task[object]] = set()
    finished = 0

    class HangingStore(HealthyStore):
        async def blocked(self) -> None:
            nonlocal finished
            task = asyncio.current_task()
            assert task is not None
            probe_tasks.add(task)
            if len(probe_tasks) == 4:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished += 1

        async def schema_version(self) -> int:
            await self.blocked()
            return 4

        async def journal_mode(self) -> str:
            await self.blocked()
            return "wal"

        async def busy_timeout(self) -> int:
            await self.blocked()
            return 5_000

        async def write_probe(self) -> None:
            await self.blocked()

    monkeypatch.setattr(routes, "_job_store", HangingStore())
    monkeypatch.setattr(routes, "_DURABLE_STORE_PROBE_TIMEOUT_SECONDS", 0.05)
    request = asyncio.create_task(client.get("/health/ready"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        if stop == "request_cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            response = await asyncio.wait_for(request, timeout=1)
            assert response.status_code == 503
            assert response.json()["durable_store"] == {"ready": False}

        assert finished == 4
        assert all(task.done() for task in probe_tasks)
    finally:
        request.cancel()
        for task in probe_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(request, *probe_tasks, return_exceptions=True)


class BlockingBot:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.task: asyncio.Task[object] | None = None
        self.failure: Exception | None = None

    async def get_me(self) -> object:
        self.calls += 1
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
            return object()
        finally:
            self.finished.set()


@pytest.mark.asyncio
async def test_cancelled_readiness_caller_does_not_cancel_shared_local_probe(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = BlockingBot()
    monkeypatch.setattr(state, "bot_app", SimpleNamespace(bot=bot))
    callers = [asyncio.create_task(client.get("/health/ready")) for _ in range(2)]
    try:
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        callers[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await callers[0]
        assert not bot.finished.is_set()
        bot.release.set()
        response = await asyncio.wait_for(callers[1], timeout=1)

        assert response.status_code == 200
        assert response.json()["local_bot_api"]["functional_probe"] is True
        assert bot.calls == 1
        assert bot.task is not None and bot.task.done()
    finally:
        bot.release.set()
        await asyncio.gather(*callers, return_exceptions=True)


@pytest.mark.asyncio
async def test_local_probe_cache_lifetime_starts_on_completion_and_recovers(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [100.0]
    bot = BlockingBot()
    secret = "test-private-local-api-token"
    monkeypatch.setattr(state, "bot_app", SimpleNamespace(bot=bot))
    monkeypatch.setattr(routes, "_local_probe_clock", lambda: now[0])
    monkeypatch.setattr(routes, "_LOCAL_API_PROBE_FRESHNESS_SECONDS", 10.0)
    request = asyncio.create_task(client.get("/health/ready"))
    try:
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        now[0] = 150.0
        bot.release.set()
        assert (await request).status_code == 200
        now[0] = 159.0
        assert (await client.get("/health/ready")).status_code == 200
        assert bot.calls == 1

        now[0] = 161.0
        bot.failure = OSError(secret)
        failed = await client.get("/health/ready")
        assert failed.status_code == 503
        assert failed.json()["local_bot_api"]["functional_probe"] is False
        assert (await client.get("/health/ready")).status_code == 503
        assert bot.calls == 2
        assert secret not in failed.text
        assert secret not in repr([vars(record) for record in caplog.records])

        now[0] = 172.0
        bot.failure = None
        assert (await client.get("/health/ready")).status_code == 200
        assert bot.calls == 3
    finally:
        bot.release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_abandoned_local_probe_times_out_and_caches_failure_after_cleanup(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = BlockingBot()
    monkeypatch.setattr(state, "bot_app", SimpleNamespace(bot=bot))
    monkeypatch.setattr(routes, "_LOCAL_API_PROBE_TIMEOUT_SECONDS", 0.05)
    request = asyncio.create_task(client.get("/health/ready"))
    try:
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not bot.finished.is_set()
        await asyncio.wait_for(bot.finished.wait(), timeout=1)

        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["local_bot_api"]["functional_probe"] is False
        assert bot.calls == 1
        assert bot.task is not None and bot.task.done()
    finally:
        bot.release.set()
        await asyncio.gather(request, return_exceptions=True)
        if routes._local_probe_state.task is not None:
            await routes._local_probe_state.task
