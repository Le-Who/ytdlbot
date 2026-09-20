"""Durable webhook inbox and delivery deduplication backed by SQLite WAL."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar


class JobState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    FAILED = "failed"


class DeliveryOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    update_id: int
    payload: dict[str, Any]
    state: JobState
    owner_id: str | None = None
    checkpoint: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedUpdate:
    job: JobRecord
    inserted: bool


@dataclass(slots=True)
class _DeliveryJobContext:
    store: JobStore
    job_id: str
    owner_id: str | None = None
    error: BaseException | None = None


_delivery_job: contextvars.ContextVar[_DeliveryJobContext | None] = (
    contextvars.ContextVar("delivery_job", default=None)
)


_TELEGRAM_UPDATE_FIELDS = frozenset(
    {
        "business_connection",
        "business_message",
        "callback_query",
        "channel_post",
        "chat_boost",
        "chat_join_request",
        "chat_member",
        "chosen_inline_result",
        "deleted_business_messages",
        "edited_business_message",
        "edited_channel_post",
        "edited_message",
        "inline_query",
        "message",
        "message_reaction",
        "message_reaction_count",
        "my_chat_member",
        "poll",
        "poll_answer",
        "pre_checkout_query",
        "purchased_paid_media",
        "removed_chat_boost",
        "shipping_query",
    }
)

_T = TypeVar("_T")


class JobStore:
    """A small one-process durable queue with explicit recovery semantics."""

    CURRENT_SCHEMA_VERSION = 3
    DEFAULT_PATH = Path(os.getenv("YTDLBOT_JOB_DB", "/srv/ytdlbot/state/jobs.sqlite3"))

    def __init__(
        self,
        path: str | Path = DEFAULT_PATH,
        *,
        busy_timeout_ms: int = 5_000,
        payload_retention_seconds: float = 7 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if payload_retention_seconds < 0:
            raise ValueError("payload_retention_seconds must not be negative")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.payload_retention_seconds = payload_retention_seconds
        self._clock = clock
        self._initialized = False
        self._initialize_lock = threading.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """Connections are operation-scoped, so closing requires no I/O."""

    async def schema_version(self) -> int:
        await self.initialize()
        return await asyncio.to_thread(self._pragma_int, "user_version")

    async def journal_mode(self) -> str:
        await self.initialize()
        return await asyncio.to_thread(self._pragma_text, "journal_mode")

    async def busy_timeout(self) -> int:
        await self.initialize()
        return await asyncio.to_thread(self._pragma_int, "busy_timeout")

    async def accept_update(self, payload: Mapping[str, Any]) -> AcceptedUpdate:
        minimal = _minimal_update_payload(payload)
        return await self._run(
            self._accept_update_sync,
            minimal,
            self._clock(),
        )

    async def get_update(self, update_id: int) -> JobRecord | None:
        return await self._run(self._get_update_sync, update_id)

    async def acquire_worker(self, owner_id: str, *, lease_seconds: float = 30) -> bool:
        _validate_owner(owner_id)
        _validate_lease(lease_seconds)
        return await self._run(
            self._acquire_worker_sync,
            owner_id,
            lease_seconds,
            self._clock(),
        )

    async def renew_worker(self, owner_id: str, *, lease_seconds: float = 30) -> bool:
        _validate_owner(owner_id)
        _validate_lease(lease_seconds)
        return await self._run(
            self._renew_worker_sync,
            owner_id,
            lease_seconds,
            self._clock(),
        )

    async def release_worker(self, owner_id: str) -> None:
        _validate_owner(owner_id)
        await self._run(self._release_worker_sync, owner_id)

    async def claim_next(
        self,
        owner_id: str,
        *,
        lease_seconds: float = 30,
    ) -> JobRecord | None:
        jobs = await self.claim_recoverable_jobs(
            owner_id,
            limit=1,
            lease_seconds=lease_seconds,
        )
        return jobs[0] if jobs else None

    async def claim_recoverable_jobs(
        self,
        owner_id: str = "recovery",
        *,
        limit: int = 100,
        lease_seconds: float = 30,
    ) -> list[JobRecord]:
        _validate_owner(owner_id)
        _validate_lease(lease_seconds)
        if limit <= 0:
            raise ValueError("limit must be positive")
        return await self._run(
            self._claim_recoverable_jobs_sync,
            owner_id,
            limit,
            lease_seconds,
            self._clock(),
        )

    async def renew_claim(
        self,
        job_id: str | int,
        owner_id: str,
        *,
        lease_seconds: float = 30,
    ) -> bool:
        _validate_owner(owner_id)
        _validate_lease(lease_seconds)
        return await self._run(
            self._renew_claim_sync,
            str(job_id),
            owner_id,
            lease_seconds,
            self._clock(),
        )

    async def checkpoint(
        self,
        job_id: str | int,
        checkpoint: Mapping[str, Any] | None = None,
        *,
        owner_id: str | None = None,
    ) -> bool:
        encoded = _encode_json(dict(checkpoint)) if checkpoint is not None else None
        return await self._run(
            self._transition_sync,
            str(job_id),
            JobState.CHECKPOINTED,
            encoded,
            None,
            self._clock(),
            (JobState.ACCEPTED, JobState.RUNNING, JobState.CHECKPOINTED),
            owner_id,
        )

    async def complete(self, job_id: str | int, *, owner_id: str | None = None) -> bool:
        return await self._run(
            self._transition_sync,
            str(job_id),
            JobState.COMPLETED,
            None,
            None,
            self._clock(),
            (JobState.RUNNING, JobState.CHECKPOINTED, JobState.ACCEPTED),
            owner_id,
        )

    async def fail(
        self,
        job_id: str | int,
        error: str,
        *,
        owner_id: str | None = None,
    ) -> bool:
        return await self._run(
            self._transition_sync,
            str(job_id),
            JobState.FAILED,
            None,
            error[:2_000],
            self._clock(),
            (JobState.RUNNING, JobState.CHECKPOINTED, JobState.ACCEPTED),
            owner_id,
        )

    async def record_delivery(
        self,
        job_id: str | int,
        outcome: DeliveryOutcome | str,
        *,
        item_key: str = "__job__",
        delivery_id: str | None = None,
    ) -> DeliveryOutcome:
        parsed = DeliveryOutcome(outcome)
        if not item_key:
            raise ValueError("item_key must not be empty")
        return await self._run(
            self._record_delivery_sync,
            str(job_id),
            item_key,
            parsed,
            delivery_id,
            self._clock(),
        )

    async def begin_delivery_attempts(
        self,
        job_id: str | int,
        item_keys: tuple[str, ...],
        *,
        owner_id: str,
    ) -> bool:
        _validate_owner(owner_id)
        if not item_keys or any(not item_key for item_key in item_keys):
            raise ValueError("item_keys must not be empty")
        return await self._run(
            self._begin_delivery_attempts_sync,
            str(job_id),
            item_keys,
            owner_id,
            self._clock(),
        )

    async def finalize_delivery_attempt(
        self,
        job_id: str | int,
        item_key: str,
        outcome: DeliveryOutcome | str,
        *,
        owner_id: str,
        delivery_id: str | None = None,
    ) -> bool:
        _validate_owner(owner_id)
        if not item_key:
            raise ValueError("item_key must not be empty")
        return await self._run(
            self._finalize_delivery_attempt_sync,
            str(job_id),
            item_key,
            DeliveryOutcome(outcome),
            owner_id,
            delivery_id,
            self._clock(),
        )

    async def delivery_outcome(
        self,
        job_id: str | int,
        *,
        item_key: str = "__job__",
    ) -> DeliveryOutcome | None:
        return await self._run(
            self._delivery_outcome_sync,
            str(job_id),
            item_key,
        )

    async def should_deliver(
        self,
        job_id: str | int,
        *,
        item_key: str = "__job__",
    ) -> bool:
        outcome = await self.delivery_outcome(job_id, item_key=item_key)
        return outcome not in {DeliveryOutcome.SUCCESS, DeliveryOutcome.UNCERTAIN}

    async def purge_expired_payloads(self) -> int:
        return await self._run(self._purge_expired_payloads_sync, self._clock())

    async def _run(self, operation: Callable[..., _T], *args: Any) -> _T:
        await self.initialize()
        return await asyncio.to_thread(operation, *args)

    def _initialize_sync(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > self.CURRENT_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"job database schema {version} is newer than supported "
                        f"schema {self.CURRENT_SCHEMA_VERSION}"
                    )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if version < 1:
                        self._migrate_to_v1(connection)
                        version = 1
                    if version < 2:
                        self._migrate_to_v2(connection)
                        version = 2
                    if version < 3:
                        self._migrate_to_v3(connection)
                        version = 3
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.close()
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _migrate_to_v1(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                update_id INTEGER NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('accepted', 'running', 'checkpointed', 'completed', 'failed')
                ),
                owner_id TEXT,
                claim_expires_at REAL,
                checkpoint TEXT,
                error TEXT,
                payload_expires_at REAL NOT NULL,
                accepted_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS jobs_recovery_idx "
            "ON jobs(state, claim_expires_at, accepted_at)"
        )

    @staticmethod
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                job_id TEXT NOT NULL,
                item_key TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('success', 'failed', 'uncertain')
                ),
                delivery_id TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (job_id, item_key)
            )
            """
        )

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(deliveries)").fetchall()
        }
        if "finalized" not in columns:
            connection.execute(
                "ALTER TABLE deliveries ADD COLUMN "
                "finalized INTEGER NOT NULL DEFAULT 1 CHECK (finalized IN (0, 1))"
            )
        if "owner_id" not in columns:
            connection.execute("ALTER TABLE deliveries ADD COLUMN owner_id TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS worker_lease (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                owner_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )

    def _pragma_int(self, name: str) -> int:
        connection = self._connect()
        try:
            return int(connection.execute(f"PRAGMA {name}").fetchone()[0])
        finally:
            connection.close()

    def _pragma_text(self, name: str) -> str:
        connection = self._connect()
        try:
            return str(connection.execute(f"PRAGMA {name}").fetchone()[0]).lower()
        finally:
            connection.close()

    def _accept_update_sync(
        self,
        payload: dict[str, Any],
        now: float,
    ) -> AcceptedUpdate:
        update_id = int(payload["update_id"])
        encoded = _encode_json(payload)
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    job_id, update_id, payload, state, payload_expires_at,
                    accepted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(update_id),
                    update_id,
                    encoded,
                    JobState.ACCEPTED,
                    now + self.payload_retention_seconds,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE update_id = ?",
                (update_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("accepted update was not persisted")
        return AcceptedUpdate(_row_to_job(row), cursor.rowcount == 1)

    def _get_update_sync(self, update_id: int) -> JobRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE update_id = ?", (update_id,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_job(row) if row is not None else None

    def _acquire_worker_sync(
        self,
        owner_id: str,
        lease_seconds: float,
        now: float,
    ) -> bool:
        with self._transaction() as connection:
            return self._acquire_worker_in_transaction(
                connection,
                owner_id,
                lease_seconds,
                now,
            )

    @staticmethod
    def _acquire_worker_in_transaction(
        connection: sqlite3.Connection,
        owner_id: str,
        lease_seconds: float,
        now: float,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO worker_lease(singleton, owner_id, expires_at)
            VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                owner_id = excluded.owner_id,
                expires_at = excluded.expires_at
            WHERE worker_lease.owner_id = excluded.owner_id
               OR worker_lease.expires_at <= ?
            """,
            (owner_id, now + lease_seconds, now),
        )
        return cursor.rowcount == 1

    def _renew_worker_sync(
        self,
        owner_id: str,
        lease_seconds: float,
        now: float,
    ) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE worker_lease SET expires_at = ? "
                "WHERE singleton = 1 AND owner_id = ? AND expires_at > ?",
                (now + lease_seconds, owner_id, now),
            )
        return cursor.rowcount == 1

    def _release_worker_sync(self, owner_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM worker_lease WHERE singleton = 1 AND owner_id = ?",
                (owner_id,),
            )

    def _claim_recoverable_jobs_sync(
        self,
        owner_id: str,
        limit: int,
        lease_seconds: float,
        now: float,
    ) -> list[JobRecord]:
        with self._transaction() as connection:
            if not self._acquire_worker_in_transaction(
                connection, owner_id, lease_seconds, now
            ):
                return []
            rows = connection.execute(
                """
                SELECT jobs.*
                FROM jobs
                WHERE (
                    jobs.state IN ('accepted', 'checkpointed')
                    OR (
                        jobs.state = 'running'
                        AND (
                            jobs.claim_expires_at IS NULL
                            OR jobs.claim_expires_at <= ?
                        )
                    )
                )
                AND NOT EXISTS (
                    SELECT 1 FROM deliveries
                    WHERE deliveries.job_id = jobs.job_id
                      AND deliveries.item_key = '__job__'
                      AND deliveries.outcome IN ('success', 'uncertain')
                )
                ORDER BY jobs.accepted_at, jobs.update_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            if not rows:
                return []
            job_ids = [str(row["job_id"]) for row in rows]
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(
                f"""
                UPDATE jobs
                SET state = 'running', owner_id = ?, claim_expires_at = ?, updated_at = ?
                WHERE job_id IN ({placeholders})
                """,
                (owner_id, now + lease_seconds, now, *job_ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM jobs WHERE job_id IN ({placeholders}) "
                "ORDER BY accepted_at, update_id",
                job_ids,
            ).fetchall()
        return [_row_to_job(row) for row in claimed]

    def _renew_claim_sync(
        self,
        job_id: str,
        owner_id: str,
        lease_seconds: float,
        now: float,
    ) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET claim_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND state = 'running' AND owner_id = ?
                """,
                (now + lease_seconds, now, job_id, owner_id),
            )
        return cursor.rowcount == 1

    def _transition_sync(
        self,
        job_id: str,
        state: JobState,
        checkpoint: str | None,
        error: str | None,
        now: float,
        from_states: tuple[JobState, ...],
        owner_id: str | None,
    ) -> bool:
        placeholders = ",".join("?" for _ in from_states)
        values: list[Any] = [state, checkpoint, error, now, job_id]
        values.extend(from_states)
        owner_guard = ""
        if owner_id is not None:
            owner_guard = """
                AND owner_id = ?
                AND claim_expires_at > ?
                AND EXISTS (
                    SELECT 1 FROM worker_lease
                    WHERE singleton = 1
                      AND worker_lease.owner_id = ?
                      AND worker_lease.expires_at > ?
                )
            """
            values.extend((owner_id, now, owner_id, now))
        with self._transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE jobs
                SET state = ?, owner_id = NULL, claim_expires_at = NULL,
                    checkpoint = COALESCE(?, checkpoint), error = ?, updated_at = ?
                WHERE job_id = ? AND state IN ({placeholders}) {owner_guard}
                """,
                values,
            )
        return cursor.rowcount == 1

    def _record_delivery_sync(
        self,
        job_id: str,
        item_key: str,
        outcome: DeliveryOutcome,
        delivery_id: str | None,
        now: float,
    ) -> DeliveryOutcome:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO deliveries(
                    job_id, item_key, outcome, delivery_id, updated_at,
                    finalized, owner_id
                )
                VALUES (?, ?, ?, ?, ?, 1, NULL)
                ON CONFLICT(job_id, item_key) DO UPDATE SET
                    outcome = CASE
                        WHEN deliveries.finalized = 1
                         AND deliveries.outcome IN ('success', 'uncertain')
                        THEN deliveries.outcome
                        ELSE excluded.outcome
                    END,
                    delivery_id = CASE
                        WHEN deliveries.finalized = 1
                         AND deliveries.outcome IN ('success', 'uncertain')
                        THEN deliveries.delivery_id
                        ELSE excluded.delivery_id
                    END,
                    updated_at = excluded.updated_at,
                    finalized = 1,
                    owner_id = NULL
                """,
                (job_id, item_key, outcome, delivery_id, now),
            )
            row = connection.execute(
                "SELECT outcome FROM deliveries WHERE job_id = ? AND item_key = ?",
                (job_id, item_key),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by transaction
            raise RuntimeError("delivery outcome was not persisted")
        return DeliveryOutcome(row["outcome"])

    def _begin_delivery_attempts_sync(
        self,
        job_id: str,
        item_keys: tuple[str, ...],
        owner_id: str,
        now: float,
    ) -> bool:
        with self._transaction() as connection:
            if not _owns_live_job(connection, job_id, owner_id, now):
                return False
            for item_key in item_keys:
                existing = connection.execute(
                    """
                    SELECT outcome, finalized FROM deliveries
                    WHERE job_id = ? AND item_key = ?
                    """,
                    (job_id, item_key),
                ).fetchone()
                if (
                    existing is not None
                    and int(existing["finalized"]) == 1
                    and existing["outcome"] in {"success", "uncertain"}
                ):
                    return False
            for item_key in item_keys:
                cursor = connection.execute(
                    """
                    INSERT INTO deliveries(
                        job_id, item_key, outcome, delivery_id, updated_at,
                        finalized, owner_id
                    ) VALUES (?, ?, 'uncertain', NULL, ?, 0, ?)
                    ON CONFLICT(job_id, item_key) DO UPDATE SET
                        outcome = 'uncertain', delivery_id = NULL,
                        updated_at = excluded.updated_at,
                        finalized = 0, owner_id = excluded.owner_id
                    WHERE deliveries.outcome = 'failed'
                       OR deliveries.finalized = 0
                    """,
                    (job_id, item_key, now, owner_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("delivery attempt reservation was not persisted")
        return True

    def _finalize_delivery_attempt_sync(
        self,
        job_id: str,
        item_key: str,
        outcome: DeliveryOutcome,
        owner_id: str,
        delivery_id: str | None,
        now: float,
    ) -> bool:
        with self._transaction() as connection:
            if not _owns_live_job(connection, job_id, owner_id, now):
                return False
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET outcome = ?, delivery_id = ?, updated_at = ?,
                    finalized = 1, owner_id = NULL
                WHERE job_id = ? AND item_key = ?
                  AND finalized = 0 AND owner_id = ?
                """,
                (outcome, delivery_id, now, job_id, item_key, owner_id),
            )
        return cursor.rowcount == 1

    def _delivery_outcome_sync(
        self,
        job_id: str,
        item_key: str,
    ) -> DeliveryOutcome | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT outcome FROM deliveries WHERE job_id = ? AND item_key = ?",
                (job_id, item_key),
            ).fetchone()
        finally:
            connection.close()
        return DeliveryOutcome(row["outcome"]) if row is not None else None

    def _purge_expired_payloads_sync(self, now: float) -> int:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT job_id, update_id FROM jobs
                WHERE state IN ('completed', 'failed')
                  AND payload_expires_at <= ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET payload = ?, payload_expires_at = ? WHERE job_id = ?",
                    (
                        _encode_json({"update_id": int(row["update_id"])}),
                        float("inf"),
                        str(row["job_id"]),
                    ),
                )
        return len(rows)

    def _transaction(self) -> _Transaction:
        return _Transaction(self._connect())


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


def _minimal_update_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_update_id = payload.get("update_id")
    if isinstance(raw_update_id, bool) or not isinstance(raw_update_id, int):
        raise TypeError("Telegram update_id must be an integer")
    minimal: dict[str, Any] = {"update_id": raw_update_id}
    for key in _TELEGRAM_UPDATE_FIELDS:
        if key in payload and payload[key] is not None:
            minimal[key] = payload[key]
    _encode_json(minimal)
    return minimal


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    checkpoint = json.loads(row["checkpoint"]) if row["checkpoint"] else None
    return JobRecord(
        id=str(row["job_id"]),
        update_id=int(row["update_id"]),
        payload=json.loads(row["payload"]),
        state=JobState(row["state"]),
        owner_id=row["owner_id"],
        checkpoint=checkpoint,
        error=row["error"],
    )


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _validate_owner(owner_id: str) -> None:
    if not owner_id:
        raise ValueError("owner_id must not be empty")


def _validate_lease(lease_seconds: float) -> None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")


def _owns_live_job(
    connection: sqlite3.Connection,
    job_id: str,
    owner_id: str,
    now: float,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM jobs
        JOIN worker_lease ON worker_lease.singleton = 1
        WHERE jobs.job_id = ?
          AND jobs.state = 'running'
          AND jobs.owner_id = ?
          AND jobs.claim_expires_at > ?
          AND worker_lease.owner_id = ?
          AND worker_lease.expires_at > ?
        """,
        (job_id, owner_id, now, owner_id, now),
    ).fetchone()
    return row is not None


@contextmanager
def delivery_job_context(
    store: JobStore,
    job_id: str | int,
    *,
    owner_id: str | None = None,
) -> Iterator[_DeliveryJobContext]:
    """Bind delivery receipts to the durable job executing in this context."""

    context = _DeliveryJobContext(store, str(job_id), owner_id)
    token = _delivery_job.set(context)
    try:
        yield context
    finally:
        _delivery_job.reset(token)


async def current_delivery_outcome(item_key: str) -> DeliveryOutcome | None:
    context = _delivery_job.get()
    if context is None:
        return None
    return await context.store.delivery_outcome(context.job_id, item_key=item_key)


async def record_current_delivery(
    item_key: str,
    outcome: DeliveryOutcome,
    *,
    delivery_id: str | None = None,
) -> None:
    context = _delivery_job.get()
    if context is None:
        return
    if context.owner_id is not None:
        finalized = await context.store.finalize_delivery_attempt(
            context.job_id,
            item_key,
            outcome,
            owner_id=context.owner_id,
            delivery_id=delivery_id,
        )
        if not finalized:
            raise RuntimeError("durable delivery attempt lost job ownership")
        return
    await context.store.record_delivery(
        context.job_id,
        outcome,
        item_key=item_key,
        delivery_id=delivery_id,
    )


async def begin_current_delivery(item_keys: tuple[str, ...]) -> None:
    context = _delivery_job.get()
    if context is None or context.owner_id is None:
        return
    started = await context.store.begin_delivery_attempts(
        context.job_id,
        item_keys,
        owner_id=context.owner_id,
    )
    if not started:
        raise RuntimeError("durable delivery attempt could not claim job ownership")


def mark_current_job_failed(error: BaseException) -> None:
    """Record framework-handled errors that would otherwise look successful."""

    context = _delivery_job.get()
    if context is not None and context.error is None:
        context.error = error
