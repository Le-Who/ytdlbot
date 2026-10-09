"""Single-worker durable inbox processing and bounded deployment drain."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.job_store import JobRecord, JobStore, delivery_job_context

logger = logging.getLogger("app.core.drain")

# Deployment allows 30 seconds of work grace inside a 45-second container
# stop grace. Cleanup has its own budget, shared by drain and worker.stop().
CLEANUP_TIMEOUT_SECONDS = 5.0


class WorkerLeaseLost(RuntimeError):
    pass


class RetryableDeliveryFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DrainResult:
    completed: int
    checkpointed: int
    cancelled: int
    timed_out: bool


class DrainController:
    """Stops new job starts while allowing webhook inserts to continue."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._draining = False
        self._active: dict[str, tuple[asyncio.Task[None], str | None]] = {}
        self._lock = asyncio.Lock()
        self._drain_task: asyncio.Task[DrainResult] | None = None
        self._cleanup_deadline: float | None = None

    @property
    def draining(self) -> bool:
        return self._draining

    async def start_job(
        self,
        job_id: str,
        operation: Callable[[], Awaitable[None]],
        *,
        owner_id: str | None = None,
    ) -> asyncio.Task[None] | None:
        async with self._lock:
            if self._draining:
                return None

            async def invoke() -> None:
                await operation()

            task: asyncio.Task[None] = asyncio.create_task(
                invoke(), name=f"durable-job-{job_id}"
            )
            self._active[job_id] = (task, owner_id)
            return task

    async def finish_job(self, job_id: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            active = self._active.get(job_id)
            if active is not None and active[0] is task:
                self._active.pop(job_id, None)

    async def drain(self, *, deadline_seconds: float) -> DrainResult:
        """Allow work grace, then at most five seconds for shutdown cleanup.

        Cancellation is cooperative: a resistant handler remains tracked and
        is not checkpointed while alive. Repeated calls share the same drain.
        ``cancelled`` counts cancellation requests, not forced terminations.
        """
        if deadline_seconds < 0:
            raise ValueError("deadline_seconds must not be negative")
        async with self._lock:
            if self._drain_task is None:
                self._draining = True
                self._drain_task = asyncio.create_task(
                    self._drain(dict(self._active), deadline_seconds)
                )
            drain_task = self._drain_task
        return await asyncio.shield(drain_task)

    async def _drain(
        self,
        active: dict[str, tuple[asyncio.Task[None], str | None]],
        work_grace: float,
    ) -> DrainResult:
        if not active:
            self._cleanup_deadline = (
                asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
            )
            return DrainResult(0, 0, 0, False)

        done, pending = await asyncio.wait(
            (task for task, _ in active.values()),
            timeout=work_grace,
        )
        self._cleanup_deadline = (
            asyncio.get_running_loop().time() + CLEANUP_TIMEOUT_SECONDS
        )
        if not pending:
            return DrainResult(len(done), 0, 0, False)

        # Stop side effects before making work recoverable by another owner.
        for task in pending:
            task.cancel()
        stopped, still_running = await asyncio.wait(
            pending, timeout=self._cleanup_remaining()
        )
        pending_jobs = [
            (job_id, owner_id)
            for job_id, (task, owner_id) in active.items()
            if task in stopped
        ]
        checkpoints = {
            asyncio.create_task(
                self.store.checkpoint(job_id, owner_id=owner_id)
            ): job_id
            for job_id, owner_id in pending_jobs
        }
        checkpointed = 0
        if checkpoints:
            finished, unfinished = await asyncio.wait(
                checkpoints, timeout=self._cleanup_remaining()
            )
            for checkpoint_task in finished:
                if checkpoint_task.cancelled():
                    continue
                error = checkpoint_task.exception()
                if error is None:
                    checkpointed += checkpoint_task.result() is True
                else:
                    logger.error(
                        "Failed to checkpoint job during drain",
                        extra={
                            "job_id": checkpoints[checkpoint_task],
                            "error_type": type(error).__name__,
                        },
                    )
            for checkpoint_task in unfinished:
                checkpoint_task.cancel()
                checkpoint_task.add_done_callback(_consume_task_result)
        if still_running:
            logger.error(
                "Drain cleanup expired with active handlers; ownership retained"
            )
        return DrainResult(len(done), checkpointed, len(pending), True)

    def _cleanup_remaining(self) -> float:
        if self._cleanup_deadline is None:
            return CLEANUP_TIMEOUT_SECONDS
        return max(0.0, self._cleanup_deadline - asyncio.get_running_loop().time())


class DurableUpdateWorker:
    """Claims and processes one durable Telegram update at a time."""

    def __init__(
        self,
        store: JobStore,
        drain_controller: DrainController,
        process_update: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        owner_id: str | None = None,
        poll_interval: float = 0.25,
        lease_seconds: float = 30,
        maintenance_interval: float = 5 * 60,
        restart_delay: float = 1,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if maintenance_interval <= 0:
            raise ValueError("maintenance_interval must be positive")
        if restart_delay <= 0:
            raise ValueError("restart_delay must be positive")
        self.store = store
        self.drain_controller = drain_controller
        self.process_update = process_update
        self.owner_id = owner_id or uuid.uuid4().hex
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.maintenance_interval = maintenance_interval
        self.restart_delay = restart_delay
        self.last_completed_job_id: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_cleanup_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None

    @property
    def shutdown_complete(self) -> bool:
        """Whether the supervisor, its handlers and owner cleanup have exited."""
        return (
            (self._start_task is None or self._start_task.done())
            and (self._task is None or self._task.done())
            and all(task.done() for task, _ in self.drain_controller._active.values())
            and (self._stop_cleanup_task is None or self._stop_cleanup_task.done())
        )

    async def wait_stopped(self) -> None:
        """Join already requested shutdown without granting another drain budget."""
        if self._stop_task is not None:
            await asyncio.shield(self._stop_task)
        if self._stop_cleanup_task is not None:
            await asyncio.shield(self._stop_cleanup_task)

    async def start(self) -> None:
        if self._stop.is_set():
            raise RuntimeError("durable update worker is stopping")
        if self._task is not None and not self._task.done():
            return
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self._start_once())
        startup = self._start_task
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            startup.cancel()
            await _settle(asyncio.gather(startup, return_exceptions=True))
            raise

    async def _acquire_worker(self) -> bool:
        acquisition = asyncio.create_task(
            self.store.acquire_worker(self.owner_id, lease_seconds=self.lease_seconds)
        )
        try:
            return await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            # A cancelled to_thread caller cannot stop SQLite from committing.
            # Its supervisor/startup remains live until ownership settles.
            await _settle(acquisition)
            raise

    async def _start_once(self) -> None:
        try:
            acquired = await self._acquire_worker()
            if not acquired:
                raise RuntimeError("durable update worker already has an active owner")
            await self._requeue_startup_failures()
            await self._run_maintenance_once()
            if self._stop.is_set():
                raise asyncio.CancelledError
        except BaseException:
            await _settle(asyncio.create_task(self.store.release_worker(self.owner_id)))
            raise
        self._task = asyncio.create_task(
            self._supervise(),
            name=f"durable-update-worker-{self.owner_id[:8]}",
        )
        self._maintenance_task = asyncio.create_task(
            self._run_maintenance(),
            name=f"durable-update-maintenance-{self.owner_id[:8]}",
        )

    async def _requeue_startup_failures(self) -> None:
        batch_size = 100
        while True:
            requeued = await self.store.requeue_failed_deliveries(limit=batch_size)
            if requeued < batch_size:
                return

    async def stop(self) -> None:
        """Share drain's cleanup budget and retain ownership of live handlers."""
        # Synchronous with respect to start's final launch check: a pending
        # startup cannot clear shutdown or launch new runtime tasks afterward.
        self._stop.set()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_once())
        await asyncio.shield(self._stop_task)

    async def _stop_once(self) -> None:
        startup = self._start_task
        if startup is not None and not startup.done():
            startup.cancel()
        task = self._task
        if not self.drain_controller.draining:
            try:
                await self.drain_controller.drain(deadline_seconds=0)
            except Exception as error:  # noqa: BLE001 - shutdown must continue
                logger.error(
                    "Durable worker drain failed during stop",
                    extra={"error_type": type(error).__name__},
                )
        if task is not None and any(
            not handler.done() for handler, _ in self.drain_controller._active.values()
        ):
            task.cancel()
        self._stop_cleanup_task = asyncio.create_task(self._finish_stop(task, startup))
        done, _ = await asyncio.wait(
            (self._stop_cleanup_task,),
            timeout=self.drain_controller._cleanup_remaining(),
        )
        if done:
            await self._stop_cleanup_task
        else:
            logger.error("Worker cleanup remains active; deferring owner release")

    async def _finish_stop(
        self, task: asyncio.Task[None] | None, startup: asyncio.Task[None] | None
    ) -> None:
        tasks = [task] if task is not None else []
        if startup is not None:
            tasks.append(startup)
        if self._maintenance_task is not None:
            tasks.append(self._maintenance_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._release_worker()
        self._maintenance_task = None
        self._task = None

    async def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run()
                return
            except asyncio.CancelledError:
                raise
            # Supervise the complete worker boundary, including update handlers
            # and store adapters; keep retrying without swallowing cancellation.
            except Exception as error:  # noqa: BLE001
                logger.error(
                    "Durable update worker failed; retrying",
                    extra={"error_type": type(error).__name__},
                )
            await self._release_worker()
            if await self._wait_for_restart():
                return
            await self._reacquire_worker()

    async def _reacquire_worker(self) -> None:
        while not self._stop.is_set():
            try:
                acquired = await self._acquire_worker()
            # An adapter failure must leave this worker unacquired, never let it
            # process without a durable lease; the restart loop retries below.
            except Exception as error:  # noqa: BLE001
                logger.error(
                    "Durable update worker reacquire failed",
                    extra={"error_type": type(error).__name__},
                )
                acquired = False
            if acquired:
                return
            if await self._wait_for_restart():
                return

    async def _release_worker(self) -> None:
        try:
            await self.store.release_worker(self.owner_id)
        # Lease release is attempted during recovery and shutdown. Record all
        # adapter failures so later cleanup phases still get their attempt.
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Durable update worker release failed",
                extra={"error_type": type(error).__name__},
            )

    async def _wait_for_restart(self) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.restart_delay)
        except TimeoutError:
            return False
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            if self.drain_controller.draining:
                await self._wait_for_poll()
                continue
            job = await self._claim_next()
            if job is None:
                renewed = await self.store.renew_worker(
                    self.owner_id,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    raise RuntimeError("durable update worker lost ownership")
                await self._wait_for_poll()
                continue
            await self._process_job(job)

    async def _claim_next(self) -> JobRecord | None:
        claim = asyncio.create_task(
            self.store.claim_next(self.owner_id, lease_seconds=self.lease_seconds)
        )
        try:
            job = await asyncio.shield(claim)
        except asyncio.CancelledError:
            job = await _settle(claim)
            if job is not None:
                await _settle(
                    asyncio.create_task(
                        self.store.checkpoint(
                            job.id, job.checkpoint, owner_id=self.owner_id
                        )
                    )
                )
            raise
        if self._stop.is_set() and job is not None:
            await _settle(
                asyncio.create_task(
                    self.store.checkpoint(
                        job.id, job.checkpoint, owner_id=self.owner_id
                    )
                )
            )
            return None
        return job

    async def _process_job(self, job: JobRecord) -> None:
        with delivery_job_context(
            self.store, job.id, owner_id=self.owner_id
        ) as execution:
            process_task = await self.drain_controller.start_job(
                job.id,
                lambda: self.process_update(job.payload),
                owner_id=self.owner_id,
            )
        if process_task is None:
            await self.store.checkpoint(job.id, job.checkpoint, owner_id=self.owner_id)
            return
        heartbeat = asyncio.create_task(
            self._heartbeat(job.id),
            name=f"durable-job-heartbeat-{job.id}",
        )
        try:
            done, _ = await asyncio.wait(
                (process_task, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                try:
                    await heartbeat
                finally:
                    process_task.cancel()
                    await asyncio.gather(process_task, return_exceptions=True)
            await process_task
            if self.drain_controller.draining and process_task.cancelling():
                raise asyncio.CancelledError
            if execution.error is not None:
                raise RuntimeError(
                    f"Telegram handler failed: {type(execution.error).__name__}"
                ) from execution.error
            if await self.store.has_failed_deliveries(job.id):
                raise RetryableDeliveryFailed("delivery has known failed items")
        except asyncio.CancelledError:
            if not process_task.done():
                process_task.cancel()
            # Repeated cancellation of the supervisor must not detach its
            # handler. Heartbeat/ownership stay live until the handler exits.
            settled = asyncio.gather(process_task, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    continue
            drain_task = self.drain_controller._drain_task
            if drain_task is None or drain_task.done():
                await self.store.checkpoint(
                    job.id, job.checkpoint, owner_id=self.owner_id
                )
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except WorkerLeaseLost:
            await self.store.checkpoint(job.id, job.checkpoint, owner_id=self.owner_id)
            raise
        except RetryableDeliveryFailed as error:
            failed = await self.store.fail(
                job.id,
                str(error),
                owner_id=self.owner_id,
            )
            if not failed:
                raise WorkerLeaseLost(
                    "durable job lease was lost before recording delivery failure"
                )
        except Exception as error:
            logger.exception(
                "Durable update failed",
                extra={"job_id": job.id, "error_type": type(error).__name__},
            )
            await self.store.fail(job.id, str(error), owner_id=self.owner_id)
        else:
            completed = await self.store.complete(job.id, owner_id=self.owner_id)
            if not completed:
                raise WorkerLeaseLost("durable job lease was lost before completion")
            self.last_completed_job_id = job.id
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.drain_controller.finish_job(job.id, process_task)

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(0.01, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                worker_ok, claim_ok = await asyncio.gather(
                    self.store.renew_worker(
                        self.owner_id,
                        lease_seconds=self.lease_seconds,
                    ),
                    self.store.renew_claim(
                        job_id,
                        self.owner_id,
                        lease_seconds=self.lease_seconds,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise WorkerLeaseLost("durable job lease renewal failed") from error
            if not worker_ok or not claim_ok:
                raise WorkerLeaseLost("durable job lease was lost")

    async def _wait_for_poll(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
        except TimeoutError:
            pass

    async def _run_maintenance(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.maintenance_interval
                )
            except TimeoutError:
                await self._run_maintenance_once()

    async def _run_maintenance_once(self) -> None:
        try:
            await self.store.purge_expired_payloads(limit=1_000)
        # Expired-payload maintenance is optional; adapter failures must remain
        # visible without terminating the worker's separate processing task.
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Durable payload maintenance failed",
                extra={"error_type": type(error).__name__},
            )


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve abandoned checkpoint results; to_thread commits remain fenced."""
    if not task.cancelled():
        task.exception()


async def _settle[T](future: asyncio.Future[T]) -> T:
    """Keep owning an already-started operation through caller cancellation."""
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            continue
    return future.result()
