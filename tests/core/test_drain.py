from __future__ import annotations

import asyncio

import pytest

from app.core.drain import DrainController, DurableUpdateWorker
from app.core.job_store import (
    DeliveryOutcome,
    JobState,
    JobStore,
    begin_current_delivery,
    mark_current_job_failed,
    record_current_delivery,
)


def update_payload(update_id: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": 42, "type": "private"},
            "text": "https://youtu.be/example",
        },
    }


async def wait_until(predicate, *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_worker_claims_one_persisted_update_and_marks_it_completed(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    processed: list[int] = []

    async def process(payload: dict[str, object]) -> None:
        processed.append(int(payload["update_id"]))

    await store.accept_update(update_payload(701))
    worker = DurableUpdateWorker(store, controller, process, poll_interval=0.005)
    await worker.start()
    await wait_until(lambda: processed == [701])
    await wait_until(
        lambda: worker.last_completed_job_id == "701",
    )
    await worker.stop()

    record = await store.get_update(701)
    assert record is not None
    assert record.state is JobState.COMPLETED


@pytest.mark.asyncio
async def test_drain_accepts_updates_but_starts_no_new_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    processed: list[int] = []

    async def process(payload: dict[str, object]) -> None:
        processed.append(int(payload["update_id"]))

    worker = DurableUpdateWorker(store, controller, process, poll_interval=0.005)
    await worker.start()
    result = await controller.drain(deadline_seconds=0.1)
    accepted = await store.accept_update(update_payload(702))
    await asyncio.sleep(0.03)
    await worker.stop()

    record = await store.get_update(702)
    assert result.timed_out is False
    assert accepted.inserted is True
    assert processed == []
    assert record is not None
    assert record.state is JobState.ACCEPTED


@pytest.mark.asyncio
async def test_drain_deadline_checkpoints_then_cancels_active_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(payload: dict[str, object]) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await store.accept_update(update_payload(703))
    worker = DurableUpdateWorker(store, controller, process, poll_interval=0.005)
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    result = await controller.drain(deadline_seconds=0.01)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await worker.stop()

    record = await store.get_update(703)
    assert result.timed_out is True
    assert result.checkpointed == 1
    assert result.cancelled == 1
    assert record is not None
    assert record.state is JobState.CHECKPOINTED


@pytest.mark.asyncio
async def test_worker_cancels_active_job_when_it_loses_its_database_lease(tmp_path):
    now = [1_700_000_000.0]

    class LosingStore(JobStore):
        async def renew_claim(
            self,
            job_id: str | int,
            owner_id: str,
            *,
            lease_seconds: float = 30,
        ) -> bool:
            now[0] += 1
            await self.acquire_worker("worker-b", lease_seconds=30)
            await self.claim_next("worker-b", lease_seconds=30)
            return False

    store = LosingStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    controller = DrainController(store)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(payload: dict[str, object]) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await store.accept_update(update_payload(704))
    worker = DurableUpdateWorker(
        store,
        controller,
        process,
        poll_interval=0.005,
        lease_seconds=0.03,
    )
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await worker.stop()

    record = await store.get_update(704)
    assert record is not None
    assert record.state is JobState.RUNNING
    assert record.owner_id == "worker-b"


@pytest.mark.asyncio
async def test_worker_stops_active_job_when_lease_renewal_errors(tmp_path):
    class ErroringStore(JobStore):
        async def renew_claim(
            self,
            job_id: str | int,
            owner_id: str,
            *,
            lease_seconds: float = 30,
        ) -> bool:
            raise OSError("state volume unavailable")

    store = ErroringStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def process(payload: dict[str, object]) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await store.accept_update(update_payload(705))
    worker = DurableUpdateWorker(
        store,
        controller,
        process,
        poll_interval=0.005,
        lease_seconds=0.03,
    )
    await worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await worker.stop()

    record = await store.get_update(705)
    assert record is not None
    assert record.state is not JobState.COMPLETED


@pytest.mark.asyncio
async def test_handler_error_reported_by_framework_does_not_complete_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)

    async def process(payload: dict[str, object]) -> None:
        mark_current_job_failed(ValueError("handler failed"))

    await store.accept_update(update_payload(706))
    worker = DurableUpdateWorker(store, controller, process, poll_interval=0.005)
    await worker.start()
    async with asyncio.timeout(1):
        while True:
            record = await store.get_update(706)
            if record is not None and record.state is JobState.FAILED:
                break
            await asyncio.sleep(0.005)
    await worker.stop()

    assert record.error == "Telegram handler failed: ValueError"


@pytest.mark.asyncio
async def test_failed_delivery_retries_once_on_next_worker_start_only(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    attempts = 0

    async def process(payload: dict[str, object]) -> None:
        nonlocal attempts
        attempts += 1
        await begin_current_delivery(("logical-item",))
        await record_current_delivery(
            "logical-item",
            DeliveryOutcome.FAILED if attempts == 1 else DeliveryOutcome.SUCCESS,
        )

    first = DurableUpdateWorker(
        store,
        DrainController(store),
        process,
        owner_id="worker-a",
        poll_interval=0.005,
    )
    await store.accept_update(update_payload(707))
    await first.start()
    async with asyncio.timeout(1):
        while True:
            record = await store.get_update(707)
            if record is not None and record.state is JobState.FAILED:
                break
            await asyncio.sleep(0.005)
    await asyncio.sleep(0.03)
    assert attempts == 1
    await first.stop()

    second = DurableUpdateWorker(
        store,
        DrainController(store),
        process,
        owner_id="worker-b",
        poll_interval=0.005,
    )
    await second.start()
    async with asyncio.timeout(1):
        while True:
            record = await store.get_update(707)
            if record is not None and record.state is JobState.COMPLETED:
                break
            await asyncio.sleep(0.005)
    await second.stop()

    assert attempts == 2


@pytest.mark.asyncio
async def test_worker_runs_payload_retention_maintenance_at_startup(tmp_path):
    class RecordingStore(JobStore):
        def __init__(self, path) -> None:
            super().__init__(path)
            self.purge_calls = 0

        async def purge_expired_payloads(self, *, limit: int = 1_000) -> int:
            self.purge_calls += 1
            return await super().purge_expired_payloads(limit=limit)

    store = RecordingStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        poll_interval=0.005,
        maintenance_interval=0.02,
    )

    await worker.start()
    await wait_until(lambda: store.purge_calls >= 1)
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_requeues_entire_failed_delivery_backlog_at_startup(tmp_path):
    class BacklogStore(JobStore):
        def __init__(self, path) -> None:
            super().__init__(path)
            self.requeue_calls = 0

        async def requeue_failed_deliveries(self, *, limit: int = 100) -> int:
            self.requeue_calls += 1
            return 100 if self.requeue_calls == 1 else 1

    store = BacklogStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        poll_interval=0.005,
    )

    await worker.start()
    await worker.stop()

    assert store.requeue_calls == 2


@pytest.mark.asyncio
async def test_worker_restarts_after_transient_claim_failure(tmp_path):
    class FlakyStore(JobStore):
        def __init__(self, path) -> None:
            super().__init__(path)
            self.claim_failures = 1

        async def claim_next(
            self,
            owner_id: str,
            *,
            lease_seconds: float = 30,
        ):
            if self.claim_failures:
                self.claim_failures -= 1
                raise OSError("temporary sqlite error")
            return await super().claim_next(
                owner_id,
                lease_seconds=lease_seconds,
            )

    store = FlakyStore(tmp_path / "jobs.sqlite3")
    processed = asyncio.Event()

    async def process(payload: dict[str, object]) -> None:
        processed.set()

    await store.accept_update(update_payload(708))
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        process,
        poll_interval=0.005,
        restart_delay=0.01,
    )

    await worker.start()
    await asyncio.wait_for(processed.wait(), timeout=1)
    await worker.stop()

    record = await store.get_update(708)
    assert record is not None
    assert record.state is JobState.COMPLETED


@pytest.mark.asyncio
async def test_worker_stop_releases_lease_when_drain_fails(tmp_path):
    class FailingDrain(DrainController):
        async def drain(self, *, deadline_seconds: float):
            raise OSError("checkpoint volume unavailable")

    store = JobStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        FailingDrain(store),
        lambda payload: asyncio.sleep(0),
        owner_id="worker-a",
        poll_interval=0.005,
    )

    await worker.start()
    await worker.stop()

    assert await store.acquire_worker("worker-b") is True
