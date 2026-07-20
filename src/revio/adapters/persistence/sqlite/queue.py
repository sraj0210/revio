"""SQLite queue leasing, recovery, and lease-owned state transitions."""

import json
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
from revio.errors import InvalidJobError


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

    async def _maintenance(self, connection: aiosqlite.Connection, now: datetime) -> int:
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
            "AND (state<>'running' OR lease_expires_at<=?)",
            (timestamp(now), timestamp(now), timestamp(now)),
        )
        return cursor.rowcount

    async def lease_next(self, worker_id: str, now: datetime) -> JobLease | None:
        signals: list[tuple[str, str | None]] = []

        async def operation(connection: aiosqlite.Connection) -> JobLease | None:
            exhausted = await self._maintenance(connection, now)
            if exhausted:
                signals.append(("dead", "attempts_exhausted"))
            for _ in range(1_000):
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
                    return None
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
                return JobLease(
                    job=job, worker_id=worker_id, attempt_number=attempt, expires_at=expires
                )
            raise RuntimeError("invalid durable queue candidate limit exceeded")

        lease = await self._connections.write(operation)
        for metric, outcome in signals:
            self._connections.metric(metric, outcome=outcome)
        return lease

    async def heartbeat(self, lease: JobLease, now: datetime) -> bool:
        expires = datetime.fromtimestamp(
            now.timestamp() + self._settings.queue_lease_seconds, tz=UTC
        )
        return await self._lease_update(
            "UPDATE queue_jobs SET lease_expires_at=?, available_at=?, updated_at=? "
            "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
            (
                timestamp(expires),
                timestamp(expires),
                timestamp(now),
                lease.job.id,
                lease.worker_id,
                lease.attempt_number,
            ),
        )

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

        changed = await self._connections.write(operation)
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

        changed = await self._connections.write(operation)
        self._connections.metric("retry_scheduled" if changed else "lease_lost")
        return changed

    async def _lease_update(self, sql: str, values: tuple[object, ...]) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(sql, values)
            changed = cursor.rowcount == 1
            return changed

        changed = await self._connections.write(operation)
        if not changed:
            self._connections.metric("lease_lost")
        return changed

    async def recover_expired(self, now: datetime) -> int:
        async def operation(connection: aiosqlite.Connection) -> int:
            result = await self._maintenance(connection, now)
            return result

        result = await self._connections.write(operation)
        if result:
            self._connections.metric("dead", outcome="attempts_exhausted")
        return result

    async def installation_state(
        self, provider_id: str, installation_id: str
    ) -> InstallationState | None:
        return await self._installations.get(provider_id, installation_id)

    async def get_job(self, job_id: str) -> QueueJob | None:
        async with self._connections.connect() as connection:
            rows = list(
                await connection.execute_fetchall("SELECT * FROM queue_jobs WHERE id=?", (job_id,))
            )
        return self._row_to_job(rows[0]) if rows else None
