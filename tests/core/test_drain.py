from __future__ import annotations

import asyncio
import threading

import pytest

from app.core import drain as drain_module
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
    idle = asyncio.Event()

    class IdleStore(JobStore):
        async def claim_next(self, *args, **kwargs):
            job = await super().claim_next(*args, **kwargs)
            if job is None:
                idle.set()
            return job

    store = IdleStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    processed: list[int] = []

    async def process(payload: dict[str, object]) -> None:
        processed.append(int(payload["update_id"]))

    worker = DurableUpdateWorker(store, controller, process, poll_interval=0.005)
    await worker.start()
    # This test covers inserts into an already idle, draining worker. Late
    # claims are covered separately by the gated real SQLite regression.
    await asyncio.wait_for(idle.wait(), 1)
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


@pytest.mark.asyncio
async def test_cancelled_worker_finishes_handler_before_checkpoint_and_restart(
    tmp_path,
):
    """Supervisor cancellation must not leave an untracked delivery handler."""
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    entered = asyncio.Event()
    finished = asyncio.Event()
    delivered = []
    handler_tasks = []

    async def process(payload):
        handler_tasks.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
            delivered.append(payload["update_id"])
        finally:
            finished.set()

    await store.accept_update(update_payload(709))
    worker = DurableUpdateWorker(store, controller, process, owner_id="first")
    await worker.start()
    await asyncio.wait_for(entered.wait(), 1)
    worker._task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await worker._task
        assert finished.is_set()
        assert all(task.done() for task in handler_tasks)
        assert not controller._active
        assert (await store.get_update(709)).state is JobState.CHECKPOINTED
        await worker.stop()
        assert await store.acquire_worker("replacement")
        retry = await store.claim_next("replacement")
        assert retry is not None and retry.id == "709"
        assert delivered == []
    finally:
        for task in handler_tasks:
            task.cancel()
        await asyncio.gather(*handler_tasks, return_exceptions=True)
        await asyncio.gather(worker.stop(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_start", [False, True])
async def test_worker_startup_failure_rolls_back_singleton_lease(
    tmp_path, cancel_start
):
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class StartupStore(JobStore):
        async def requeue_failed_deliveries(self, *, limit=100):
            entered.set()
            await proceed.wait()
            raise OSError("recovery unavailable")

    store = StartupStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        owner_id="first",
    )
    start = asyncio.create_task(worker.start())
    await asyncio.wait_for(entered.wait(), 1)
    if cancel_start:
        start.cancel()
    else:
        proceed.set()
    with pytest.raises(asyncio.CancelledError if cancel_start else OSError):
        await start
    try:
        assert worker._task is None
        assert worker._maintenance_task is None
        assert await store.acquire_worker("replacement")
    finally:
        await store.release_worker("first")
        await store.release_worker("replacement")


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_acquisition_before_rolling_back(tmp_path):
    """Cancellation cannot let an acquisition commit after its rollback."""
    acquired = asyncio.Event()
    proceed = asyncio.Event()

    class DelayedAcquireStore(JobStore):
        async def acquire_worker(self, owner_id, *, lease_seconds=30):
            result = await super().acquire_worker(owner_id, lease_seconds=lease_seconds)
            if owner_id == "first":
                acquired.set()
                await proceed.wait()
            return result

    store = DelayedAcquireStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        owner_id="first",
    )
    start = asyncio.create_task(worker.start())
    await acquired.wait()
    start.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    try:
        assert await store.acquire_worker("replacement")
        assert worker._task is None
    finally:
        await store.release_worker("first")
        await store.release_worker("replacement")


@pytest.mark.asyncio
async def test_timed_out_checkpoint_thread_cannot_mutate_replacement_owner(
    tmp_path, monkeypatch
):
    """A to_thread commit may outlive cancellation, so owner fencing is required."""
    # Give the executor time to begin the real transaction boundary. The slow
    # coroutine test covers a tight drain deadline without executor scheduling.
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.3)
    checkpoint_started = threading.Event()
    checkpoint_finished = threading.Event()
    commit_allowed = threading.Event()
    now = [100.0]

    class DelayedCommitStore(JobStore):
        def _transition_sync(self, job_id, state, *args):
            if state is JobState.CHECKPOINTED:
                checkpoint_started.set()
                if not commit_allowed.wait(2):
                    raise RuntimeError("test did not release checkpoint")
                try:
                    return super()._transition_sync(job_id, state, *args)
                finally:
                    checkpoint_finished.set()
            return super()._transition_sync(job_id, state, *args)

    store = DelayedCommitStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update(update_payload(712))
    assert await store.claim_next("first") is not None
    controller = DrainController(store)
    entered = asyncio.Event()

    async def process():
        entered.set()
        await asyncio.Event().wait()

    task = await controller.start_job("712", process, owner_id="first")
    await entered.wait()
    draining = asyncio.create_task(controller.drain(deadline_seconds=0))
    try:
        assert await asyncio.to_thread(checkpoint_started.wait, 1)
        result = await draining
        assert result.checkpointed == 0
        assert task.done()
        await store.release_worker("first")
        now[0] = 131
        assert await store.acquire_worker("replacement")
        recovered = await store.claim_next("replacement")
        assert recovered is not None and recovered.id == "712"
        commit_allowed.set()
        assert await asyncio.to_thread(checkpoint_finished.wait, 1)
        record = await store.get_update(712)
        assert record.state is JobState.RUNNING
        assert record.owner_id == "replacement"
    finally:
        commit_allowed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(draining, return_exceptions=True)
        await asyncio.to_thread(checkpoint_finished.wait, 1)


@pytest.mark.asyncio
async def test_slow_checkpoint_is_bounded_after_work_grace(tmp_path, monkeypatch):
    """A checkpoint cannot consume the container's remaining shutdown grace."""
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.03, raising=False)
    entered = asyncio.Event()
    finished = asyncio.Event()
    checkpoint_started = asyncio.Event()
    checkpoint_cancelled = asyncio.Event()

    class SlowStore(JobStore):
        async def checkpoint(self, *args, **kwargs):
            checkpoint_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                checkpoint_cancelled.set()

    store = SlowStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)

    async def process():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = await controller.start_job("slow", process)
    await entered.wait()
    try:
        async with asyncio.timeout(0.3):
            result = await controller.drain(deadline_seconds=0.01)
        assert finished.is_set()
        assert checkpoint_started.is_set()
        assert result.timed_out and result.checkpointed == 0
        assert result.cancelled == 1
        async with asyncio.timeout(0.3):
            await checkpoint_cancelled.wait()
            assert await controller.drain(deadline_seconds=10) == result
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resistant_handler_stays_tracked_and_owned_after_bounded_stop(
    tmp_path, monkeypatch
):
    """Bounded stop requests cancellation but cannot abandon a live handler."""
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.03, raising=False)
    store = JobStore(tmp_path / "jobs.sqlite3")
    controller = DrainController(store)
    entered = asyncio.Event()
    cancel_received = asyncio.Event()
    finish = asyncio.Event()
    handler_tasks = []

    async def process(payload):
        handler_tasks.append(asyncio.current_task())
        entered.set()
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancel_received.set()

    await store.accept_update(update_payload(710))
    worker = DurableUpdateWorker(
        store, controller, process, owner_id="first", poll_interval=0.005
    )
    await worker.start()
    await entered.wait()

    async def shutdown():
        result = await controller.drain(deadline_seconds=0)
        await worker.stop()
        return result

    shutdown_task = asyncio.create_task(shutdown())
    try:
        done, _ = await asyncio.wait((shutdown_task,), timeout=0.3)
        assert shutdown_task in done, "drain/stop exceeded bounded cleanup"
        result = shutdown_task.result()
        assert result.timed_out and result.checkpointed == 0
        assert cancel_received.is_set()
        assert not handler_tasks[0].done()
        assert "710" in controller._active
        assert (await store.get_update(710)).state is JobState.RUNNING
        assert await store.acquire_worker("replacement") is False
        # Repeated shutdown calls must not buy another work/cleanup grace.
        async with asyncio.timeout(0.02):
            assert await controller.drain(deadline_seconds=10) == result
            await worker.stop()
    finally:
        finish.set()
        await asyncio.gather(*handler_tasks, return_exceptions=True)
        await asyncio.gather(shutdown_task, return_exceptions=True)
        if worker._task is not None:
            await asyncio.gather(worker._task, return_exceptions=True)
        await worker.stop()
    await wait_until(lambda: worker._task is None)
    assert not controller._active
    assert await store.acquire_worker("replacement")


@pytest.mark.asyncio
async def test_worker_stop_cancels_active_handler_when_drain_raises(tmp_path):
    class FailingDrain(DrainController):
        async def drain(self, *, deadline_seconds):
            raise OSError("checkpoint volume unavailable")

    store = JobStore(tmp_path / "jobs.sqlite3")
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def process(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    await store.accept_update(update_payload(711))
    controller = FailingDrain(store)
    worker = DurableUpdateWorker(store, controller, process, owner_id="first")
    await worker.start()
    await entered.wait()
    try:
        async with asyncio.timeout(0.3):
            await worker.stop()
        assert finished.is_set()
        assert not controller._active
        assert await store.acquire_worker("replacement")
    finally:
        if worker._task is not None:
            worker._task.cancel()
            await asyncio.gather(worker._task, return_exceptions=True)
        for task, _ in controller._active.values():
            task.cancel()
        await asyncio.gather(
            *(task for task, _ in controller._active.values()), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["claim", "restart-acquisition"])
async def test_cancelled_sqlite_ownership_operation_settles_before_release(
    tmp_path, monkeypatch, operation
):
    """A stopped supervisor cannot regain ownership through a late thread commit."""
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.03)
    entered = threading.Event()
    allow_commit = threading.Event()
    committed = threading.Event()
    handlers_started = []

    class GatedStore(JobStore):
        acquire_calls = 0
        claim_calls = 0

        def _claim_recoverable_jobs_sync(self, *args):
            if operation == "claim":
                entered.set()
                assert allow_commit.wait(2), "test did not release claim thread"
                try:
                    return super()._claim_recoverable_jobs_sync(*args)
                finally:
                    committed.set()
            return super()._claim_recoverable_jobs_sync(*args)

        def _acquire_worker_sync(self, *args):
            self.acquire_calls += 1
            if operation == "restart-acquisition" and self.acquire_calls == 2:
                entered.set()
                assert allow_commit.wait(2), "test did not release acquisition thread"
                try:
                    return super()._acquire_worker_sync(*args)
                finally:
                    committed.set()
            return super()._acquire_worker_sync(*args)

        async def claim_next(self, *args, **kwargs):
            self.claim_calls += 1
            if operation == "restart-acquisition" and self.claim_calls == 1:
                raise OSError("restart to exercise reacquisition")
            return await super().claim_next(*args, **kwargs)

    store = GatedStore(tmp_path / "jobs.sqlite3")
    await store.accept_update(update_payload(713))

    async def process(payload):
        handlers_started.append(payload["update_id"])

    worker = DurableUpdateWorker(
        store, DrainController(store), process, owner_id="first", restart_delay=0.005
    )
    await worker.start()
    supervisor = worker._task
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        supervisor.cancel()
        async with asyncio.timeout(0.3):
            await worker.stop()
        completed_before_commit = worker.shutdown_complete
        allow_commit.set()
        assert await asyncio.to_thread(committed.wait, 1)
        await asyncio.gather(supervisor, return_exceptions=True)
        await worker.wait_stopped()
        assert await store.acquire_worker("replacement"), (
            "late commit restored stopped owner"
        )
        assert not completed_before_commit
        record = await store.get_update(713)
        assert record.state is (
            JobState.CHECKPOINTED if operation == "claim" else JobState.ACCEPTED
        )
        assert record.owner_id is None
        assert handlers_started == []
        assert worker.shutdown_complete
    finally:
        allow_commit.set()
        await asyncio.to_thread(committed.wait, 1)
        if worker._task is not None:
            worker._task.cancel()
            await asyncio.gather(worker._task, return_exceptions=True)
        await worker.stop()
        await worker.wait_stopped()
        await store.release_worker("first")
        await store.release_worker("replacement")


@pytest.mark.asyncio
async def test_stop_during_sqlite_startup_prevents_late_runtime_tasks(
    tmp_path, monkeypatch
):
    """A pending acquisition belongs to shutdown until it settles."""
    monkeypatch.setattr(drain_module, "CLEANUP_TIMEOUT_SECONDS", 0.03)
    entered = threading.Event()
    allow_commit = threading.Event()
    committed = threading.Event()

    class GatedStore(JobStore):
        def _acquire_worker_sync(self, *args):
            if args[0] == "first":
                entered.set()
                assert allow_commit.wait(2), "test did not release startup thread"
                try:
                    return super()._acquire_worker_sync(*args)
                finally:
                    committed.set()
            return super()._acquire_worker_sync(*args)

    store = GatedStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        owner_id="first",
    )
    startup = asyncio.create_task(worker.start())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        async with asyncio.timeout(0.3):
            await worker.stop()
        completed_before_commit = worker.shutdown_complete
        allow_commit.set()
        assert await asyncio.to_thread(committed.wait, 1)
        await asyncio.gather(startup, return_exceptions=True)
        assert worker._task is None, "startup launched a supervisor after stop"
        assert worker._maintenance_task is None
        assert worker._stop.is_set()
        assert not completed_before_commit
        await worker.wait_stopped()
        assert worker.shutdown_complete
        assert await store.acquire_worker("replacement")
    finally:
        allow_commit.set()
        await asyncio.gather(startup, return_exceptions=True)
        tasks = [
            task
            for task in (worker._task, worker._maintenance_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await worker.stop()
        await worker.wait_stopped()
        await store.release_worker("first")
        await store.release_worker("replacement")


@pytest.mark.asyncio
async def test_concurrent_start_calls_share_one_supervisor_and_maintenance(tmp_path):
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class GatedRecoveryStore(JobStore):
        async def requeue_failed_deliveries(self, *, limit=100):
            entered.set()
            await proceed.wait()
            return await super().requeue_failed_deliveries(limit=limit)

    store = GatedRecoveryStore(tmp_path / "jobs.sqlite3")
    worker = DurableUpdateWorker(
        store,
        DrainController(store),
        lambda payload: asyncio.sleep(0),
        owner_id="concurrent",
    )
    first = asyncio.create_task(worker.start())
    second = None
    runtime_tasks = []
    try:
        await entered.wait()
        second = asyncio.create_task(worker.start())
        await asyncio.sleep(0)
        proceed.set()
        await asyncio.gather(first, second)
        runtime_tasks = [
            task
            for task in asyncio.all_tasks()
            if task.get_name()
            in {"durable-update-worker-concurre", "durable-update-maintenance-concurre"}
        ]
        assert len(runtime_tasks) == 2, (
            "concurrent starts created duplicate runtime tasks"
        )
        await worker.stop()
        assert all(task.done() for task in runtime_tasks)
        assert worker.shutdown_complete
        assert await store.acquire_worker("replacement")
    finally:
        proceed.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
        await worker.stop()
        for task in runtime_tasks:
            task.cancel()
        await asyncio.gather(*runtime_tasks, return_exceptions=True)
        await store.release_worker("concurrent")
        await store.release_worker("replacement")
