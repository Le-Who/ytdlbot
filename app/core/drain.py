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


class WorkerLeaseLost(RuntimeError):
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
        if deadline_seconds < 0:
            raise ValueError("deadline_seconds must not be negative")
        async with self._lock:
            self._draining = True
            active = dict(self._active)
        if not active:
            return DrainResult(0, 0, 0, False)

        done, pending = await asyncio.wait(
            (task for task, _ in active.values()),
            timeout=deadline_seconds,
        )
        if not pending:
            return DrainResult(len(done), 0, 0, False)

        pending_jobs = [
            (job_id, owner_id)
            for job_id, (task, owner_id) in active.items()
            if task in pending
        ]
        checkpoint_results = await asyncio.gather(
            *(
                self.store.checkpoint(job_id, owner_id=owner_id)
                for job_id, owner_id in pending_jobs
            ),
            return_exceptions=True,
        )
        checkpointed = sum(result is True for result in checkpoint_results)
        for (job_id, _), result in zip(pending_jobs, checkpoint_results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "Failed to checkpoint job during drain",
                    extra={"job_id": job_id, "error_type": type(result).__name__},
                )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return DrainResult(len(done), checkpointed, len(pending), True)


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
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.store = store
        self.drain_controller = drain_controller
        self.process_update = process_update
        self.owner_id = owner_id or uuid.uuid4().hex
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.last_completed_job_id: str | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        acquired = await self.store.acquire_worker(
            self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        if not acquired:
            raise RuntimeError("durable update worker already has an active owner")
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"durable-update-worker-{self.owner_id[:8]}",
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            await self.store.release_worker(self.owner_id)
            return
        if not self.drain_controller.draining:
            await self.drain_controller.drain(deadline_seconds=0)
        self._stop.set()
        try:
            await task
        finally:
            await self.store.release_worker(self.owner_id)
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            if self.drain_controller.draining:
                await self._wait_for_poll()
                continue
            job = await self.store.claim_next(
                self.owner_id,
                lease_seconds=self.lease_seconds,
            )
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
            if execution.error is not None:
                raise RuntimeError(
                    f"Telegram handler failed: {type(execution.error).__name__}"
                ) from execution.error
        except asyncio.CancelledError:
            await self.store.checkpoint(job.id, job.checkpoint, owner_id=self.owner_id)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except WorkerLeaseLost:
            await self.store.checkpoint(job.id, job.checkpoint, owner_id=self.owner_id)
            raise
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
