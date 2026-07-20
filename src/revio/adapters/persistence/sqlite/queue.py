"""SQLite queue leasing, recovery, and lease-owned state transitions."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import aiosqlite
from pydantic import ValidationError

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.installations import SQLiteInstallationRepository
from revio.adapters.persistence.sqlite.values import datetime_value, timestamp
from revio.config.queue import QueueSettings
from revio.domain.events import ReviewEvent
from revio.domain.queue import InstallationState, JobLease, QueueJob
from revio.errors import InvalidJobError, PersistenceIntegrityError, PersistenceNotCommittedError

INVALID_JOB_CLEANUP_BATCH_SIZE = 64


@dataclass(frozen=True)
class _LeaseBatch:
    lease: JobLease | None
    invalid_ids: tuple[str, ...]
    exhausted_ids: tuple[str, ...]
    queue_empty: bool


class SQLiteQueueRepository:
    def __init__(
        self,
        connections: SQLiteConnectionPolicy,
        installations: SQLiteInstallationRepository,
        settings: QueueSettings,
    ) -> None:
        self._connections = connections
        self._installations = installations
        self._settings = settings

    @staticmethod
    def _decode_event(row: aiosqlite.Row) -> ReviewEvent:
        if row["event_schema_version"] != 1:
            raise InvalidJobError("persisted job schema is invalid")
        try:
            payload = json.loads(cast(str, row["event_json"]))
            return ReviewEvent.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            raise InvalidJobError("persisted job schema is invalid") from None

    @staticmethod
    def _row_to_job(row: aiosqlite.Row, event: ReviewEvent | None = None) -> QueueJob:
        return QueueJob(
            id=row["id"],
            provider_id=row["provider_id"],
            job_type=row["job_type"],
            semantic_identity=row["semantic_identity"],
            event_schema_version=row["event_schema_version"],
            event=event or SQLiteQueueRepository._decode_event(row),
            state=row["state"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=cast(datetime, datetime_value(row["available_at"])),
            lease_owner=row["lease_owner"],
            lease_expires_at=datetime_value(row["lease_expires_at"]),
            terminal_at=datetime_value(row["terminal_at"]),
        )

    async def _maintenance(
        self, connection: aiosqlite.Connection, now: datetime
    ) -> tuple[str, ...]:
        await connection.execute(
            "UPDATE job_attempts SET finished_at=?, outcome='lease_expired' "
            "WHERE finished_at IS NULL AND job_id IN ("
            "SELECT id FROM queue_jobs WHERE state='running' AND lease_expires_at<=?)",
            (timestamp(now), timestamp(now)),
        )
        cursor = await connection.execute(
            "UPDATE queue_jobs SET state='dead', terminal_at=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL, "
            "terminal_reason=CASE WHEN state='running' "
            "THEN 'lease_expired_attempts_exhausted' ELSE 'attempts_exhausted' END, "
            "safe_error_class='AttemptLimitExceeded', "
            "safe_error_message='queue attempts exhausted' "
            "WHERE state IN ('pending','running','retry_wait') "
            "AND attempt_count>=max_attempts "
            "AND (state<>'running' OR lease_expires_at<=?) RETURNING id",
            (timestamp(now), timestamp(now), timestamp(now)),
        )
        return tuple(row["id"] for row in await cursor.fetchall())

    async def _reconcile_lease_batch(self, expected: _LeaseBatch) -> _LeaseBatch:
        async def read(connection: aiosqlite.Connection) -> _LeaseBatch:
            expected_terminal = (*expected.invalid_ids, *expected.exhausted_ids)
            confirmed_terminal = 0
            if expected_terminal:
                placeholders = ",".join("?" for _ in expected_terminal)
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT id, state, terminal_reason, terminal_at, lease_owner, "
                        "lease_expires_at, safe_error_class, safe_error_message "
                        f"FROM queue_jobs WHERE id IN ({placeholders})",
                        expected_terminal,
                    )
                )
                by_id = {row["id"]: row for row in rows}
                for job_id in expected.invalid_ids:
                    row = by_id.get(job_id)
                    if row is None or row["state"] != "dead":
                        continue
                    if (
                        row["terminal_reason"] != "invalid_job_schema"
                        or row["terminal_at"] is None
                        or row["lease_owner"] is not None
                        or row["lease_expires_at"] is not None
                        or row["safe_error_class"] != "InvalidJobError"
                        or row["safe_error_message"] != "persisted job schema is invalid"
                    ):
                        raise PersistenceIntegrityError(
                            "queue commit reconciliation found inconsistent state"
                        )
                    attempts = list(
                        await connection.execute_fetchall(
                            "SELECT 1 FROM job_attempts "
                            "WHERE job_id=? AND finished_at IS NULL LIMIT 1",
                            (job_id,),
                        )
                    )
                    if attempts:
                        raise PersistenceIntegrityError(
                            "queue commit reconciliation found inconsistent state"
                        )
                    confirmed_terminal += 1
                for job_id in expected.exhausted_ids:
                    row = by_id.get(job_id)
                    if row is not None and row["state"] == "dead" and row["terminal_at"]:
                        confirmed_terminal += 1
            lease_confirmed = expected.lease is None
            if expected.lease is not None:
                lease = expected.lease
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT state, lease_owner, attempt_count FROM queue_jobs WHERE id=?",
                        (lease.job.id,),
                    )
                )
                attempts = list(
                    await connection.execute_fetchall(
                        "SELECT finished_at FROM job_attempts "
                        "WHERE job_id=? AND attempt_number=? AND worker_id=?",
                        (lease.job.id, lease.attempt_number, lease.worker_id),
                    )
                )
                lease_confirmed = bool(
                    rows
                    and rows[0]["state"] == "running"
                    and rows[0]["lease_owner"] == lease.worker_id
                    and rows[0]["attempt_count"] == lease.attempt_number
                    and attempts
                    and attempts[0]["finished_at"] is None
                )
                if rows and not lease_confirmed:
                    raise PersistenceIntegrityError(
                        "queue commit reconciliation found inconsistent state"
                    )
            expected_count = len(expected_terminal)
            if confirmed_terminal == expected_count and lease_confirmed:
                return expected
            if confirmed_terminal == 0 and not lease_confirmed:
                raise PersistenceNotCommittedError("queue commit was not confirmed")
            if expected.lease is None and confirmed_terminal == 0 and expected_count:
                raise PersistenceNotCommittedError("queue commit was not confirmed")
            raise PersistenceIntegrityError("queue commit reconciliation found inconsistent state")

        return await self._connections.read(read)

    async def lease_next(self, worker_id: str, now: datetime) -> JobLease | None:
        while True:
            signals: list[tuple[str, str | None]] = []

            async def operation(
                connection: aiosqlite.Connection,
                signals: list[tuple[str, str | None]] = signals,
            ) -> _LeaseBatch:
                exhausted_ids = await self._maintenance(connection, now)
                if exhausted_ids:
                    signals.append(("dead", "attempts_exhausted"))
                invalid_ids: list[str] = []
                for _ in range(INVALID_JOB_CLEANUP_BATCH_SIZE):
                    candidates = list(
                        await connection.execute_fetchall(
                            "SELECT * FROM queue_jobs WHERE "
                            "((state IN ('pending','retry_wait') AND available_at<=?) "
                            "OR (state='running' AND lease_expires_at<=?)) "
                            "AND attempt_count<max_attempts "
                            "ORDER BY available_at, created_at, id LIMIT 1",
                            (timestamp(now), timestamp(now)),
                        )
                    )
                    if not candidates:
                        return _LeaseBatch(
                            lease=None,
                            invalid_ids=tuple(invalid_ids),
                            exhausted_ids=exhausted_ids,
                            queue_empty=True,
                        )
                    candidate = candidates[0]
                    try:
                        event = self._decode_event(candidate)
                    except InvalidJobError:
                        await connection.execute(
                            "UPDATE queue_jobs SET state='dead', terminal_at=?, updated_at=?, "
                            "lease_owner=NULL, lease_expires_at=NULL, "
                            "terminal_reason='invalid_job_schema', "
                            "safe_error_class='InvalidJobError', "
                            "safe_error_message='persisted job schema is invalid' "
                            "WHERE id=? AND state IN ('pending','running','retry_wait')",
                            (timestamp(now), timestamp(now), candidate["id"]),
                        )
                        invalid_ids.append(candidate["id"])
                        signals.append(("dead", "invalid_job_schema"))
                        continue

                    expires = datetime.fromtimestamp(
                        now.timestamp() + self._settings.queue_lease_seconds, tz=UTC
                    )
                    cursor = await connection.execute(
                        "UPDATE queue_jobs SET state='running', lease_owner=?, lease_expires_at=?, "
                        "available_at=?, attempt_count=attempt_count+1, "
                        "first_started_at=COALESCE(first_started_at,?), last_started_at=?, "
                        "updated_at=? WHERE id=? AND "
                        "((state IN ('pending','retry_wait') AND available_at<=?) "
                        "OR (state='running' AND lease_expires_at<=?)) "
                        "AND attempt_count<max_attempts RETURNING *",
                        (
                            worker_id,
                            timestamp(expires),
                            timestamp(expires),
                            timestamp(now),
                            timestamp(now),
                            timestamp(now),
                            candidate["id"],
                            timestamp(now),
                            timestamp(now),
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        continue
                    self._connections.fail("after_lease_update")
                    attempt = cast(int, row["attempt_count"])
                    await connection.execute(
                        "INSERT INTO job_attempts "
                        "(job_id, attempt_number, worker_id, started_at) VALUES (?, ?, ?, ?)",
                        (row["id"], attempt, worker_id, timestamp(now)),
                    )
                    self._connections.fail("after_attempt_insert")
                    if candidate["state"] == "running":
                        signals.append(("lease_recovered", None))
                    signals.append(("lease_acquired", None))
                    job = self._row_to_job(row, event)
                    lease = JobLease(
                        job=job,
                        worker_id=worker_id,
                        attempt_number=attempt,
                        expires_at=expires,
                    )
                    return _LeaseBatch(
                        lease=lease,
                        invalid_ids=tuple(invalid_ids),
                        exhausted_ids=exhausted_ids,
                        queue_empty=False,
                    )
                return _LeaseBatch(
                    lease=None,
                    invalid_ids=tuple(invalid_ids),
                    exhausted_ids=exhausted_ids,
                    queue_empty=False,
                )

            batch = await self._connections.write(operation, reconcile=self._reconcile_lease_batch)
            for metric, outcome in signals:
                self._connections.metric(metric, outcome=outcome)
            if batch.lease is not None:
                return batch.lease
            if batch.queue_empty:
                return None

    async def heartbeat(self, lease: JobLease, now: datetime) -> bool:
        expires = datetime.fromtimestamp(
            now.timestamp() + self._settings.queue_lease_seconds, tz=UTC
        )
        expected_expiry = timestamp(expires)

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE queue_jobs SET lease_expires_at=?, available_at=?, updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
                (
                    expected_expiry,
                    expected_expiry,
                    timestamp(now),
                    lease.job.id,
                    lease.worker_id,
                    lease.attempt_number,
                ),
            )
            return cursor.rowcount == 1

        async def reconcile(expected: bool) -> bool:
            async def read(connection: aiosqlite.Connection) -> bool:
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT state, lease_owner, lease_expires_at, attempt_count "
                        "FROM queue_jobs WHERE id=?",
                        (lease.job.id,),
                    )
                )
                if not expected:
                    return False
                if (
                    rows
                    and rows[0]["state"] == "running"
                    and rows[0]["lease_owner"] == lease.worker_id
                    and rows[0]["attempt_count"] == lease.attempt_number
                    and rows[0]["lease_expires_at"] == expected_expiry
                ):
                    return True
                raise PersistenceNotCommittedError("queue heartbeat commit was not confirmed")

            return await self._connections.read(read)

        changed = await self._connections.write(operation, reconcile=reconcile)
        if not changed:
            self._connections.metric("lease_lost")
        return changed

    async def complete(
        self, lease: JobLease, *, head_sha: str, base_sha: str, now: datetime
    ) -> bool:
        return await self._finish(
            lease, state="completed", now=now, head_sha=head_sha, base_sha=base_sha
        )

    async def terminate(self, lease: JobLease, state: str, reason: str, now: datetime) -> bool:
        if state not in {"dead", "cancelled", "superseded"}:
            raise ValueError("invalid terminal queue state")
        return await self._finish(lease, state=state, now=now, reason=reason)

    async def _finish(
        self,
        lease: JobLease,
        *,
        state: str,
        now: datetime,
        reason: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE queue_jobs SET state=?, terminal_at=?, updated_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL, terminal_reason=?, "
                "observed_head_sha=?, observed_base_sha=? "
                "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
                (
                    state,
                    timestamp(now),
                    timestamp(now),
                    reason or state,
                    head_sha,
                    base_sha,
                    lease.job.id,
                    lease.worker_id,
                    lease.attempt_number,
                ),
            )
            changed = cursor.rowcount == 1
            if changed:
                await connection.execute(
                    "UPDATE job_attempts SET finished_at=?, outcome=?, error_message=? "
                    "WHERE job_id=? AND attempt_number=?",
                    (timestamp(now), state, reason, lease.job.id, lease.attempt_number),
                )
            return changed

        async def reconcile(expected: bool) -> bool:
            async def read(connection: aiosqlite.Connection) -> bool:
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT state, terminal_at, lease_owner, lease_expires_at "
                        "FROM queue_jobs WHERE id=?",
                        (lease.job.id,),
                    )
                )
                if not expected:
                    return False
                attempts = list(
                    await connection.execute_fetchall(
                        "SELECT outcome, finished_at FROM job_attempts "
                        "WHERE job_id=? AND attempt_number=?",
                        (lease.job.id, lease.attempt_number),
                    )
                )
                if (
                    rows
                    and rows[0]["state"] == state
                    and rows[0]["terminal_at"] is not None
                    and rows[0]["lease_owner"] is None
                    and rows[0]["lease_expires_at"] is None
                    and attempts
                    and attempts[0]["outcome"] == state
                    and attempts[0]["finished_at"] is not None
                ):
                    return True
                if rows and rows[0]["state"] == "running":
                    raise PersistenceNotCommittedError("queue transition commit was not confirmed")
                raise PersistenceIntegrityError(
                    "queue commit reconciliation found inconsistent state"
                )

            return await self._connections.read(read)

        changed = await self._connections.write(operation, reconcile=reconcile)
        self._connections.metric(state if changed else "lease_lost")
        return changed

    async def retry(
        self,
        lease: JobLease,
        *,
        available_at: datetime,
        error_class: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        if lease.attempt_number >= lease.job.max_attempts:
            return await self.terminate(lease, "dead", "retry attempts exhausted", now)

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE queue_jobs SET state='retry_wait', available_at=?, updated_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL, "
                "safe_error_class=?, safe_error_message=? "
                "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
                (
                    timestamp(available_at),
                    timestamp(now),
                    error_class[:128],
                    error_message[:500],
                    lease.job.id,
                    lease.worker_id,
                    lease.attempt_number,
                ),
            )
            changed = cursor.rowcount == 1
            if changed:
                await connection.execute(
                    "UPDATE job_attempts SET finished_at=?, outcome='retry', "
                    "error_class=?, error_message=?, retry_delay_seconds=? "
                    "WHERE job_id=? AND attempt_number=?",
                    (
                        timestamp(now),
                        error_class[:128],
                        error_message[:500],
                        max(0.0, available_at.timestamp() - now.timestamp()),
                        lease.job.id,
                        lease.attempt_number,
                    ),
                )
            return changed

        async def reconcile(expected: bool) -> bool:
            async def read(connection: aiosqlite.Connection) -> bool:
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT state, available_at, lease_owner, lease_expires_at "
                        "FROM queue_jobs WHERE id=?",
                        (lease.job.id,),
                    )
                )
                if not expected:
                    return False
                attempts = list(
                    await connection.execute_fetchall(
                        "SELECT outcome, finished_at FROM job_attempts "
                        "WHERE job_id=? AND attempt_number=?",
                        (lease.job.id, lease.attempt_number),
                    )
                )
                if (
                    rows
                    and rows[0]["state"] == "retry_wait"
                    and rows[0]["available_at"] == timestamp(available_at)
                    and rows[0]["lease_owner"] is None
                    and rows[0]["lease_expires_at"] is None
                    and attempts
                    and attempts[0]["outcome"] == "retry"
                    and attempts[0]["finished_at"] is not None
                ):
                    return True
                if rows and rows[0]["state"] == "running":
                    raise PersistenceNotCommittedError("queue retry commit was not confirmed")
                raise PersistenceIntegrityError(
                    "queue commit reconciliation found inconsistent state"
                )

            return await self._connections.read(read)

        changed = await self._connections.write(operation, reconcile=reconcile)
        self._connections.metric("retry_scheduled" if changed else "lease_lost")
        return changed

    async def recover_expired(self, now: datetime) -> int:
        async def operation(connection: aiosqlite.Connection) -> tuple[str, ...]:
            return await self._maintenance(connection, now)

        async def reconcile(expected: tuple[str, ...]) -> tuple[str, ...]:
            async def read(connection: aiosqlite.Connection) -> tuple[str, ...]:
                if not expected:
                    return expected
                placeholders = ",".join("?" for _ in expected)
                rows = list(
                    await connection.execute_fetchall(
                        "SELECT id, state, terminal_at FROM queue_jobs "
                        f"WHERE id IN ({placeholders})",
                        expected,
                    )
                )
                confirmed = {
                    row["id"]
                    for row in rows
                    if row["state"] == "dead" and row["terminal_at"] is not None
                }
                if confirmed == set(expected):
                    return expected
                if not confirmed:
                    raise PersistenceNotCommittedError("queue recovery commit was not confirmed")
                raise PersistenceIntegrityError(
                    "queue commit reconciliation found inconsistent state"
                )

            return await self._connections.read(read)

        exhausted_ids = await self._connections.write(operation, reconcile=reconcile)
        result = len(exhausted_ids)
        if result:
            self._connections.metric("dead", outcome="attempts_exhausted")
        return result

    async def installation_state(
        self, provider_id: str, installation_id: str
    ) -> InstallationState | None:
        return await self._installations.get(provider_id, installation_id)

    async def get_job(self, job_id: str) -> QueueJob | None:
        async def read(connection: aiosqlite.Connection) -> list[aiosqlite.Row]:
            return list(
                await connection.execute_fetchall("SELECT * FROM queue_jobs WHERE id=?", (job_id,))
            )

        rows = await self._connections.read(read)
        return self._row_to_job(rows[0]) if rows else None
