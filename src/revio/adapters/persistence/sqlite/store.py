"""Bounded, transaction-safe SQLite delivery and queue repository."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import aiosqlite

from revio.adapters.persistence.sqlite.capabilities import require_supported_sqlite
from revio.adapters.persistence.sqlite.schema import (
    ACTIVE_JOB_INSERT_SQL,
    LEASE_SQL,
    SCHEMA_REVISION,
    SCHEMA_SQL,
)
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.events import ReviewEvent, WebhookNormalizationResult
from revio.domain.queue import (
    IngressDisposition,
    IngressReceipt,
    InstallationState,
    InstallationStatus,
    JobLease,
    QueueJob,
)
from revio.errors import InvalidJobError, PersistenceUnavailableError, QueueCapacityError

T = TypeVar("T")


def timestamp(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


_timestamp = timestamp


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _is_busy(error: aiosqlite.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


class SQLiteStore:
    """One-writer durable ingress and atomic queue operations."""

    def __init__(self, database: DatabaseSettings, queue: QueueSettings | None = None) -> None:
        require_supported_sqlite()
        self._database = database
        self._queue = queue or QueueSettings()

    @property
    def path(self) -> Path:
        return self._database.database_path

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        connection = await aiosqlite.connect(self.path, timeout=0)
        self.path.chmod(0o600)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute(f"PRAGMA busy_timeout={self._database.database_busy_timeout_ms}")
        if self._database.database_wal_enabled:
            await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute(f"PRAGMA synchronous={self._database.database_synchronous}")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        """Create the Phase 3 schema for development/tests and stamp its revision."""
        directory_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not directory_existed:
            self.path.parent.chmod(0o700)
        async with self.connect() as connection:
            if self._database.database_wal_enabled:
                await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute(f"PRAGMA synchronous={self._database.database_synchronous}")
            await connection.executescript(SCHEMA_SQL)
            await connection.execute("DELETE FROM alembic_version")
            await connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)", (SCHEMA_REVISION,)
            )
            await connection.commit()

    async def _bounded_write(self, operation: Callable[[], Awaitable[T]]) -> T:
        started = time.monotonic()
        for attempt in range(1, self._database.database_busy_max_attempts + 1):
            remaining = self._database.database_busy_max_elapsed_seconds - (
                time.monotonic() - started
            )
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(operation(), timeout=remaining)
            except aiosqlite.OperationalError as error:
                if not _is_busy(error):
                    raise
                if attempt >= self._database.database_busy_max_attempts:
                    break
                remaining = self._database.database_busy_max_elapsed_seconds - (
                    time.monotonic() - started
                )
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05 * attempt, remaining))
            except TimeoutError:
                break
        raise PersistenceUnavailableError("database is temporarily unavailable") from None

    async def persist(
        self,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
    ) -> IngressReceipt:
        async def operation() -> IngressReceipt:
            async with self.connect() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    receipt = await self._persist_transaction(
                        connection,
                        provider_id=provider_id,
                        delivery_identity=delivery_identity,
                        event_name=event_name,
                        payload_sha256=payload_sha256,
                        normalization=normalization,
                        received_at=received_at,
                    )
                    await connection.commit()
                    return receipt
                except BaseException:
                    await connection.rollback()
                    raise

        return await self._bounded_write(operation)

    async def _persist_transaction(
        self,
        connection: aiosqlite.Connection,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
    ) -> IngressReceipt:
        tombstone = await connection.execute_fetchall(
            "SELECT payload_sha256 FROM webhook_delivery_tombstones "
            "WHERE provider_id=? AND delivery_identity=?",
            (provider_id, delivery_identity),
        )
        tombstone = list(tombstone)
        if tombstone:
            disposition = (
                IngressDisposition.IDEMPOTENT
                if tombstone[0]["payload_sha256"] == payload_sha256
                else IngressDisposition.CONFLICT
            )
            return IngressReceipt(disposition=disposition)

        delivery_id = str(uuid.uuid4())
        event = normalization.event
        event_json = event.model_dump_json() if event is not None else None
        if (
            event_json is not None
            and len(event_json.encode("utf-8")) > self._queue.queue_max_event_json_bytes
        ):
            raise InvalidJobError("normalized event exceeds the durable size limit")
        cursor = await connection.execute(
            """
            INSERT INTO webhook_deliveries (
                id, provider_id, delivery_identity, event_name, payload_sha256,
                disposition, ignored_reason, event_schema_version,
                normalized_event_json, semantic_identity, received_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, delivery_identity) DO NOTHING RETURNING id
            """,
            (
                delivery_id,
                provider_id,
                delivery_identity,
                event_name,
                payload_sha256,
                normalization.disposition,
                normalization.reason,
                1 if event is not None else None,
                event_json,
                event.semantic_identity if event is not None else None,
                _timestamp(received_at),
                _timestamp(received_at),
            ),
        )
        inserted = await cursor.fetchone()
        if inserted is None:
            existing = await connection.execute_fetchall(
                "SELECT payload_sha256, linked_job_id FROM webhook_deliveries "
                "WHERE provider_id=? AND delivery_identity=?",
                (provider_id, delivery_identity),
            )
            existing = list(existing)
            if not existing or existing[0]["payload_sha256"] != payload_sha256:
                return IngressReceipt(disposition=IngressDisposition.CONFLICT)
            return IngressReceipt(
                disposition=IngressDisposition.IDEMPOTENT,
                job_id=cast(str | None, existing[0]["linked_job_id"]),
            )

        if normalization.disposition == "ignored" or event is None:
            return IngressReceipt(disposition=IngressDisposition.IGNORED)
        if event.event_type == "installation":
            await self._update_installation(connection, delivery_id, event, received_at)
            return IngressReceipt(disposition=IngressDisposition.ACCEPTED)

        active = await connection.execute_fetchall(
            "SELECT id FROM queue_jobs WHERE provider_id=? AND job_type=? "
            "AND semantic_identity=? AND state IN ('pending','running','retry_wait')",
            (provider_id, "change_request_validation", event.semantic_identity),
        )
        active = list(active)
        if active:
            job_id = cast(str, active[0]["id"])
        else:
            count = await connection.execute_fetchall(
                "SELECT COUNT(*) AS total FROM queue_jobs "
                "WHERE state IN ('pending','running','retry_wait')"
            )
            count = list(count)
            if cast(int, count[0]["total"]) >= self._queue.queue_max_active_jobs:
                raise QueueCapacityError("durable queue capacity is exhausted")
            job_id = str(uuid.uuid4())
            cursor = await connection.execute(
                ACTIVE_JOB_INSERT_SQL,
                (
                    job_id,
                    provider_id,
                    "change_request_validation",
                    event.semantic_identity,
                    event_json,
                    self._queue.queue_max_attempts,
                    _timestamp(received_at),
                    _timestamp(received_at),
                    _timestamp(received_at),
                ),
            )
            inserted_job = await cursor.fetchone()
            if inserted_job is None:
                canonical = await connection.execute_fetchall(
                    "SELECT id FROM queue_jobs WHERE provider_id=? AND job_type=? "
                    "AND semantic_identity=? AND state IN ('pending','running','retry_wait')",
                    (provider_id, "change_request_validation", event.semantic_identity),
                )
                canonical = list(canonical)
                job_id = cast(str, canonical[0]["id"])
        await connection.execute(
            "UPDATE webhook_deliveries SET linked_job_id=? WHERE id=?", (job_id, delivery_id)
        )
        return IngressReceipt(disposition=IngressDisposition.ACCEPTED, job_id=job_id)

    async def _update_installation(
        self,
        connection: aiosqlite.Connection,
        delivery_id: str,
        event: ReviewEvent,
        now: datetime,
    ) -> None:
        status = {
            "created": InstallationStatus.ACTIVE,
            "unsuspend": InstallationStatus.ACTIVE,
            "suspend": InstallationStatus.SUSPENDED,
            "deleted": InstallationStatus.DELETED,
        }[event.trigger]
        existing = await connection.execute_fetchall(
            "SELECT provider_updated_at, ordering_delivery_identity "
            "FROM installation_states WHERE provider_id=? AND installation_id=?",
            (str(event.provider_id), event.installation.external_id),
        )
        existing = list(existing)
        should_update = True
        incoming = event.provider_updated_at
        if existing and incoming is not None and existing[0]["provider_updated_at"] is not None:
            current = datetime.fromisoformat(cast(str, existing[0]["provider_updated_at"]))
            should_update = incoming > current or (
                incoming == current
                and event.delivery_identity > existing[0]["ordering_delivery_identity"]
            )
        if not should_update:
            return
        await connection.execute(
            """
            INSERT INTO installation_states (
                provider_id, installation_id, state, source_delivery_id,
                provider_updated_at, ordering_delivery_identity, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, installation_id) DO UPDATE SET
                state=excluded.state, source_delivery_id=excluded.source_delivery_id,
                provider_updated_at=excluded.provider_updated_at,
                ordering_delivery_identity=excluded.ordering_delivery_identity,
                updated_at=excluded.updated_at
            """,
            (
                str(event.provider_id),
                event.installation.external_id,
                status,
                delivery_id,
                _timestamp(incoming) if incoming is not None else None,
                event.delivery_identity,
                _timestamp(now),
            ),
        )

    async def lease_next(self, worker_id: str, now: datetime) -> JobLease | None:
        async def operation() -> JobLease | None:
            async with self.connect() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                expires = datetime.fromtimestamp(
                    now.timestamp() + self._queue.queue_lease_seconds, tz=UTC
                )
                await connection.execute(
                    "UPDATE job_attempts SET finished_at=?, outcome='lease_expired' "
                    "WHERE finished_at IS NULL AND job_id IN ("
                    "SELECT id FROM queue_jobs WHERE state='running' AND lease_expires_at<=?)",
                    (_timestamp(now), _timestamp(now)),
                )
                await connection.execute(
                    "UPDATE queue_jobs SET state='dead', terminal_at=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "terminal_reason=CASE WHEN state='running' "
                    "THEN 'lease_expired_attempts_exhausted' ELSE 'attempts_exhausted' END "
                    "WHERE state IN ('pending','running','retry_wait') "
                    "AND attempt_count>=max_attempts "
                    "AND (state<>'running' OR lease_expires_at<=?)",
                    (_timestamp(now), _timestamp(now), _timestamp(now)),
                )
                await connection.execute(
                    "UPDATE queue_jobs SET state='dead', terminal_at=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "terminal_reason='invalid_job_schema' "
                    "WHERE event_schema_version<>1 AND "
                    "(state IN ('pending','retry_wait') "
                    "OR (state='running' AND lease_expires_at<=?))",
                    (_timestamp(now), _timestamp(now), _timestamp(now)),
                )
                cursor = await connection.execute(
                    LEASE_SQL,
                    (
                        worker_id,
                        _timestamp(expires),
                        _timestamp(expires),
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    await connection.commit()
                    return None
                attempt = cast(int, row["attempt_count"])
                await connection.execute(
                    "INSERT INTO job_attempts "
                    "(job_id, attempt_number, worker_id, started_at) VALUES (?, ?, ?, ?)",
                    (row["id"], attempt, worker_id, _timestamp(now)),
                )
                await connection.commit()
                job = self._row_to_job(row)
                return JobLease(
                    job=job, worker_id=worker_id, attempt_number=attempt, expires_at=expires
                )

        return await self._bounded_write(operation)

    async def heartbeat(self, lease: JobLease, now: datetime) -> bool:
        expires = datetime.fromtimestamp(now.timestamp() + self._queue.queue_lease_seconds, tz=UTC)
        return await self._lease_update(
            "UPDATE queue_jobs SET lease_expires_at=?, available_at=?, updated_at=? "
            "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
            (
                _timestamp(expires),
                _timestamp(expires),
                _timestamp(now),
                lease.job.id,
                lease.worker_id,
                lease.attempt_number,
            ),
        )

    async def complete(
        self, lease: JobLease, *, head_sha: str, base_sha: str, now: datetime
    ) -> bool:
        return await self._finish(
            lease,
            state="completed",
            now=now,
            head_sha=head_sha,
            base_sha=base_sha,
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
        async def operation() -> bool:
            async with self.connect() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    "UPDATE queue_jobs SET state=?, terminal_at=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, terminal_reason=?, "
                    "observed_head_sha=?, observed_base_sha=? "
                    "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
                    (
                        state,
                        _timestamp(now),
                        _timestamp(now),
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
                        (
                            _timestamp(now),
                            state,
                            reason,
                            lease.job.id,
                            lease.attempt_number,
                        ),
                    )
                await connection.commit()
                return changed

        return await self._bounded_write(operation)

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

        async def operation() -> bool:
            async with self.connect() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    "UPDATE queue_jobs SET state='retry_wait', available_at=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "safe_error_class=?, safe_error_message=? "
                    "WHERE id=? AND state='running' AND lease_owner=? AND attempt_count=?",
                    (
                        _timestamp(available_at),
                        _timestamp(now),
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
                            _timestamp(now),
                            error_class[:128],
                            error_message[:500],
                            max(0.0, available_at.timestamp() - now.timestamp()),
                            lease.job.id,
                            lease.attempt_number,
                        ),
                    )
                await connection.commit()
                return changed

        return await self._bounded_write(operation)

    async def _lease_update(self, sql: str, values: tuple[object, ...]) -> bool:
        async def operation() -> bool:
            async with self.connect() as connection:
                cursor = await connection.execute(sql, values)
                await connection.commit()
                return cursor.rowcount == 1

        return await self._bounded_write(operation)

    async def recover_expired(self, now: datetime) -> int:
        async def operation() -> int:
            async with self.connect() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                await connection.execute(
                    "UPDATE job_attempts SET finished_at=?, outcome='lease_expired' "
                    "WHERE finished_at IS NULL AND job_id IN ("
                    "SELECT id FROM queue_jobs WHERE state='running' AND lease_expires_at<=?)",
                    (_timestamp(now), _timestamp(now)),
                )
                cursor = await connection.execute(
                    "UPDATE queue_jobs SET state='dead', terminal_at=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "terminal_reason=CASE WHEN state='running' "
                    "THEN 'lease_expired_attempts_exhausted' ELSE 'attempts_exhausted' END "
                    "WHERE state IN ('pending','running','retry_wait') "
                    "AND attempt_count>=max_attempts "
                    "AND (state<>'running' OR lease_expires_at<=?)",
                    (_timestamp(now), _timestamp(now), _timestamp(now)),
                )
                await connection.commit()
                return cursor.rowcount

        return await self._bounded_write(operation)

    async def installation_state(
        self, provider_id: str, installation_id: str
    ) -> InstallationState | None:
        async with self.connect() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM installation_states WHERE provider_id=? AND installation_id=?",
                (provider_id, installation_id),
            )
            rows = list(rows)
        if not rows:
            return None
        row = rows[0]
        return InstallationState(
            provider_id=row["provider_id"],
            installation_id=row["installation_id"],
            state=row["state"],
            provider_updated_at=_datetime(row["provider_updated_at"]),
            ordering_delivery_identity=row["ordering_delivery_identity"],
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    async def get_job(self, job_id: str) -> QueueJob | None:
        async with self.connect() as connection:
            rows = await connection.execute_fetchall(
                "SELECT * FROM queue_jobs WHERE id=?", (job_id,)
            )
            rows = list(rows)
        return self._row_to_job(rows[0]) if rows else None

    @staticmethod
    def _row_to_job(row: aiosqlite.Row) -> QueueJob:
        payload = cast(dict[str, Any], json.loads(cast(str, row["event_json"])))
        return QueueJob(
            id=row["id"],
            provider_id=row["provider_id"],
            job_type=row["job_type"],
            semantic_identity=row["semantic_identity"],
            event_schema_version=row["event_schema_version"],
            event=ReviewEvent.model_validate(payload),
            state=row["state"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=cast(datetime, _datetime(row["available_at"])),
            lease_owner=row["lease_owner"],
            lease_expires_at=_datetime(row["lease_expires_at"]),
            terminal_at=_datetime(row["terminal_at"]),
        )

    async def check_ready(self) -> bool:
        try:
            return await self._bounded_write(self._check_ready_once)
        except PersistenceUnavailableError:
            return False

    async def _check_ready_once(self) -> bool:
        require_supported_sqlite()
        try:
            async with self.connect() as connection:
                revision = await connection.execute_fetchall(
                    "SELECT version_num FROM alembic_version"
                )
                revision = list(revision)
                if not revision or revision[0]["version_num"] != SCHEMA_REVISION:
                    return False
                foreign_keys = await connection.execute_fetchall("PRAGMA foreign_keys")
                foreign_keys = list(foreign_keys)
                if foreign_keys[0][0] != 1:
                    return False
                journal_mode = list(await connection.execute_fetchall("PRAGMA journal_mode"))
                if self._database.database_wal_enabled and journal_mode[0][0] != "wal":
                    return False
                synchronous = list(await connection.execute_fetchall("PRAGMA synchronous"))
                expected_synchronous = 2 if self._database.database_synchronous == "FULL" else 1
                if synchronous[0][0] != expected_synchronous:
                    return False
                overlaps = await connection.execute_fetchall(
                    "SELECT 1 FROM webhook_deliveries d JOIN webhook_delivery_tombstones t "
                    "ON d.provider_id=t.provider_id "
                    "AND d.delivery_identity=t.delivery_identity LIMIT 1"
                )
                overlaps = list(overlaps)
                if overlaps:
                    return False
                cursor = await connection.execute(
                    "UPDATE queue_jobs SET updated_at=updated_at WHERE 0 RETURNING id"
                )
                capability = await cursor.fetchone()
                await connection.rollback()
                return capability is None
        except aiosqlite.OperationalError as error:
            if _is_busy(error):
                raise
            return False
        except (aiosqlite.Error, OSError):
            return False

    async def status(self) -> dict[str, int]:
        async with self.connect() as connection:
            rows = await connection.execute_fetchall(
                "SELECT state, COUNT(*) AS total FROM queue_jobs GROUP BY state"
            )
            rows = list(rows)
            deliveries = await connection.execute_fetchall(
                "SELECT COUNT(*) AS total FROM webhook_deliveries"
            )
            deliveries = list(deliveries)
            tombstones = await connection.execute_fetchall(
                "SELECT COUNT(*) AS total FROM webhook_delivery_tombstones"
            )
            tombstones = list(tombstones)
        result = {cast(str, row["state"]): cast(int, row["total"]) for row in rows}
        result["deliveries"] = cast(int, deliveries[0]["total"])
        result["tombstones"] = cast(int, tombstones[0]["total"])
        result["active_jobs"] = sum(
            result.get(state, 0) for state in ("pending", "running", "retry_wait")
        )
        result["terminal_jobs"] = sum(
            result.get(state, 0) for state in ("completed", "dead", "cancelled", "superseded")
        )
        result["database_bytes"] = self.path.stat().st_size
        return result
