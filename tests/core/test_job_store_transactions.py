from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.core.job_store import DeliveryOutcome, JobState, JobStore


class ObservedConnection(sqlite3.Connection):
    closed = False
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("synthetic disk full during commit")
        super().commit()

    def close(self) -> None:
        self.closed = True
        super().close()


def observe_connections(monkeypatch, store: JobStore, *, fail_commit=False):
    connections: list[ObservedConnection] = []

    def connect() -> ObservedConnection:
        connection = sqlite3.connect(
            store.path,
            timeout=store.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
            factory=ObservedConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.fail_commit = fail_commit
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    return connections


async def running_job(path: Path) -> JobStore:
    store = JobStore(path)
    await store.accept_update({"update_id": 701})
    assert await store.acquire_worker("worker", lease_seconds=60)
    assert await store.claim_next("worker", lease_seconds=60) is not None
    return store


async def test_locked_admission_closes_failed_transaction_and_remains_retryable(
    tmp_path, monkeypatch
):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path, busy_timeout_ms=20)
    await store.initialize()
    connections = observe_connections(monkeypatch, store)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await store.accept_update({"update_id": 700})
            assert len(connections) == 1
            assert connections[0].closed
        finally:
            writer.rollback()
            for connection in connections:
                if not connection.closed:
                    connection.close()

    reopened = JobStore(path)
    assert await reopened.get_update(700) is None
    accepted = await reopened.accept_update({"update_id": 700})
    assert accepted.inserted
    assert accepted.job.state is JobState.ACCEPTED


async def test_admission_commit_failure_cannot_acknowledge_or_deduplicate_lost_update(
    tmp_path, monkeypatch
):
    path = tmp_path / "jobs.sqlite3"
    store = JobStore(path)
    await store.initialize()
    connections = observe_connections(monkeypatch, store, fail_commit=True)

    with pytest.raises(sqlite3.OperationalError, match="during commit"):
        await store.accept_update({"update_id": 702})

    assert len(connections) == 1
    assert connections[0].closed
    reopened = JobStore(path)
    assert await reopened.get_update(702) is None
    assert (await reopened.accept_update({"update_id": 702})).inserted
    assert not (await reopened.accept_update({"update_id": 702})).inserted


async def test_album_reservation_failure_rolls_back_every_item_before_retry(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = await running_job(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TRIGGER reject_second_item BEFORE INSERT ON deliveries "
            "WHEN NEW.item_key = 'second' BEGIN "
            "SELECT RAISE(ABORT, 'synthetic second item failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="second item failure"):
        await store.begin_delivery_attempts(
            "701", ("first", "second", "third"), owner_id="worker"
        )

    reopened = JobStore(path)
    for item_key in ("first", "second", "third"):
        assert await reopened.delivery_outcome("701", item_key=item_key) is None
    assert not await reopened.has_unsafe_deliveries("701")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TRIGGER reject_second_item")
    assert await reopened.begin_delivery_attempts(
        "701", ("first", "second", "third"), owner_id="worker"
    )
    for item_key in ("first", "second", "third"):
        assert (
            await reopened.delivery_outcome("701", item_key=item_key)
            is DeliveryOutcome.UNCERTAIN
        )


@pytest.mark.parametrize("outcome", [DeliveryOutcome.SUCCESS, DeliveryOutcome.FAILED])
async def test_receipt_commit_failure_preserves_uncertainty_across_restart(
    tmp_path, monkeypatch, outcome
):
    path = tmp_path / "jobs.sqlite3"
    store = await running_job(path)
    assert await store.begin_delivery_attempts("701", ("item",), owner_id="worker")
    connections = observe_connections(monkeypatch, store, fail_commit=True)

    with pytest.raises(sqlite3.OperationalError, match="during commit"):
        await store.finalize_delivery_attempt(
            "701", "item", outcome, owner_id="worker", delivery_id="message:123"
        )

    assert len(connections) == 1
    assert connections[0].closed
    reopened = JobStore(path)
    assert (
        await reopened.delivery_outcome("701", item_key="item")
        is DeliveryOutcome.UNCERTAIN
    )
    assert await reopened.has_unsafe_deliveries("701")
    assert not await reopened.should_deliver("701", item_key="item")
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT outcome, finalized, delivery_id FROM deliveries "
            "WHERE job_id = '701' AND item_key = 'item'"
        ).fetchone()
    assert row == ("uncertain", 0, None)
