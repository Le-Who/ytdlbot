from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.job_store import DeliveryOutcome, JobState, JobStore


def update_payload(update_id: int, *, text: str = "hello") -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": 42, "type": "private"},
            "text": text,
        },
        "debug_only": {"cookie": "must-not-be-retained"},
    }


@pytest.mark.asyncio
async def test_store_uses_wal_busy_timeout_and_explicit_schema_version(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3", busy_timeout_ms=2_345)

    await store.initialize()

    assert await store.schema_version() == store.CURRENT_SCHEMA_VERSION
    assert await store.journal_mode() == "wal"
    assert await store.busy_timeout() == 2_345


@pytest.mark.asyncio
async def test_update_id_is_deduplicated_and_payload_keeps_only_telegram_update(
    tmp_path,
):
    store = JobStore(tmp_path / "jobs.sqlite3")

    first = await store.accept_update(update_payload(101, text="first"))
    duplicate = await store.accept_update(update_payload(101, text="second"))
    persisted = await store.get_update(101)

    assert first.inserted is True
    assert duplicate.inserted is False
    assert persisted is not None
    assert persisted.state is JobState.ACCEPTED
    assert persisted.payload == {
        "update_id": 101,
        "message": {
            "message_id": 101,
            "date": 1_700_000_000,
            "chat": {"id": 42, "type": "private"},
            "text": "first",
        },
    }


@pytest.mark.asyncio
async def test_recovery_skips_success_and_uncertain_but_retries_failed_delivery(
    tmp_path,
):
    store = JobStore(tmp_path / "jobs.sqlite3")
    for update_id in (201, 202, 203):
        await store.accept_update(update_payload(update_id))
    await store.record_delivery("201", DeliveryOutcome.SUCCESS)
    await store.record_delivery("202", DeliveryOutcome.UNCERTAIN)
    await store.record_delivery("203", DeliveryOutcome.FAILED)

    recovered = await store.claim_recoverable_jobs("worker-a")

    assert {job.id for job in recovered} == {"203"}
    assert await store.should_deliver("201") is False
    assert await store.should_deliver("202") is False
    assert await store.should_deliver("203") is True


@pytest.mark.asyncio
async def test_only_one_worker_owner_can_claim_jobs(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = JobStore(path)
    second = JobStore(path)
    await first.accept_update(update_payload(301))

    assert await first.acquire_worker("worker-a", lease_seconds=30) is True
    assert await second.acquire_worker("worker-b", lease_seconds=30) is False
    assert await second.claim_next("worker-b") is None

    await first.release_worker("worker-a")
    claimed = await second.claim_next("worker-b")
    assert claimed is not None
    assert claimed.update_id == 301
    assert claimed.state is JobState.RUNNING


@pytest.mark.asyncio
async def test_stale_owner_cannot_checkpoint_or_complete_after_lease_takeover(tmp_path):
    now = [1_700_000_000.0]
    store = JobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    await store.accept_update(update_payload(302))
    first = await store.claim_next("worker-a", lease_seconds=1)
    assert first is not None
    now[0] += 2
    taken_over = await store.claim_next("worker-b", lease_seconds=30)
    assert taken_over is not None

    assert await store.checkpoint("302", owner_id="worker-a") is False
    assert await store.complete("302", owner_id="worker-a") is False

    current = await store.get_update(302)
    assert current is not None
    assert current.state is JobState.RUNNING
    assert current.owner_id == "worker-b"


@pytest.mark.asyncio
async def test_checkpointed_job_is_recovered_after_worker_restart(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = JobStore(path)
    await first.accept_update(update_payload(401))
    claimed = await first.claim_next("old-worker")
    assert claimed is not None
    await first.checkpoint(claimed.id, {"stage": "downloaded"})
    await first.release_worker("old-worker")

    reopened = JobStore(path)
    recovered = await reopened.claim_next("new-worker")

    assert recovered is not None
    assert recovered.id == "401"
    assert recovered.checkpoint == {"stage": "downloaded"}


@pytest.mark.asyncio
async def test_running_job_without_a_live_claim_is_recovered(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    await store.accept_update(update_payload(402))
    await store.initialize()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE jobs SET state = 'running', owner_id = NULL, claim_expires_at = NULL "
        "WHERE update_id = 402"
    )
    connection.commit()
    connection.close()

    recovered = await store.claim_next("new-worker")

    assert recovered is not None
    assert recovered.id == "402"


@pytest.mark.asyncio
async def test_terminal_payload_is_removed_after_retention_deadline(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3", payload_retention_seconds=0)
    await store.accept_update(update_payload(501))
    await store.complete("501")

    assert await store.purge_expired_payloads() == 1
    record = await store.get_update(501)
    assert record is not None
    assert record.payload == {"update_id": 501}


@pytest.mark.asyncio
async def test_version_one_database_migrates_without_losing_accepted_update(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            update_id INTEGER NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            state TEXT NOT NULL,
            owner_id TEXT,
            claim_expires_at REAL,
            checkpoint TEXT,
            error TEXT,
            payload_expires_at REAL NOT NULL,
            accepted_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)",
        (
            "601",
            601,
            json.dumps(update_payload(601)),
            "accepted",
            1_800_000_000.0,
            1_700_000_000.0,
            1_700_000_000.0,
        ),
    )
    connection.commit()
    connection.close()

    store = JobStore(path)
    await store.initialize()

    assert await store.schema_version() == store.CURRENT_SCHEMA_VERSION
    assert (await store.get_update(601)) is not None
    await store.record_delivery("601", DeliveryOutcome.SUCCESS)
    assert await store.should_deliver("601") is False
