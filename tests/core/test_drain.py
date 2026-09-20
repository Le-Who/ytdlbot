from __future__ import annotations

import asyncio

import pytest

from app.core.drain import DrainController, DurableUpdateWorker
from app.core.job_store import JobState, JobStore, mark_current_job_failed


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
    with pytest.raises(RuntimeError, match="lease"):
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
    with pytest.raises(RuntimeError, match="lease"):
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
