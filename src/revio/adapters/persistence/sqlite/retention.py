"""Atomic terminal-history retention and permanent delivery tombstones."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import timestamp
from revio.domain.queue import RetentionResult
from revio.errors import PersistenceUnavailableError, RetentionIntegrityError

logger = logging.getLogger(__name__)


class SQLiteTerminalRetentionRepository:
    def __init__(
        self,
        connections: SQLiteConnectionPolicy,
        capability_check: Callable[[], Awaitable[bool]],
    ) -> None:
        self._connections = connections
        self._capability_check = capability_check

    async def retain_terminal_history(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        dry_run: bool,
        correlation_id: str,
    ) -> RetentionResult:
        if not await self._capability_check():
            raise PersistenceUnavailableError("database is not ready for retention") from None

        async def operation(connection: aiosqlite.Connection) -> RetentionResult:
            jobs = list(
                await connection.execute_fetchall(
                    "SELECT id FROM queue_jobs "
                    "WHERE state IN ('completed','dead','cancelled','superseded') "
                    "AND terminal_at<? ORDER BY terminal_at, id LIMIT ?",
                    (timestamp(cutoff), batch_size),
                )
            )
            job_ids = [row["id"] for row in jobs]
            if not job_ids:
                return RetentionResult(jobs=0, attempts=0, deliveries=0)
            placeholders = ",".join("?" for _ in job_ids)
            attempts = next(
                iter(
                    await connection.execute_fetchall(
                        "SELECT COUNT(*) AS total FROM job_attempts "
                        f"WHERE job_id IN ({placeholders})",
                        job_ids,
                    )
                )
            )["total"]
            deliveries = list(
                await connection.execute_fetchall(
                    "SELECT id, provider_id, delivery_identity, payload_sha256 "
                    f"FROM webhook_deliveries WHERE linked_job_id IN ({placeholders})",
                    job_ids,
                )
            )
            result = RetentionResult(jobs=len(jobs), attempts=attempts, deliveries=len(deliveries))
            if dry_run:
                return result

            retained_at = timestamp(datetime.now(UTC))
            for row in deliveries:
                cursor = await connection.execute(
                    "INSERT INTO webhook_delivery_tombstones "
                    "(provider_id, delivery_identity, payload_sha256, retained_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(provider_id, delivery_identity) DO NOTHING "
                    "RETURNING provider_id",
                    (
                        row["provider_id"],
                        row["delivery_identity"],
                        row["payload_sha256"],
                        retained_at,
                    ),
                )
                if await cursor.fetchone() is not None:
                    continue
                existing = list(
                    await connection.execute_fetchall(
                        "SELECT payload_sha256 FROM webhook_delivery_tombstones "
                        "WHERE provider_id=? AND delivery_identity=?",
                        (row["provider_id"], row["delivery_identity"]),
                    )
                )
                if not existing or existing[0]["payload_sha256"] != row["payload_sha256"]:
                    logger.error(
                        "terminal_retention_integrity_conflict",
                        extra={"correlation_id": correlation_id},
                    )
                    self._connections.metric("delivery_integrity_conflict")
                    raise RetentionIntegrityError(
                        "terminal retention found a delivery integrity conflict"
                    ) from None
            self._connections.fail("after_tombstone_verification")
            for row in deliveries:
                await connection.execute("DELETE FROM webhook_deliveries WHERE id=?", (row["id"],))
            await connection.execute(
                f"DELETE FROM job_attempts WHERE job_id IN ({placeholders})", job_ids
            )
            await connection.execute(
                f"DELETE FROM queue_jobs WHERE id IN ({placeholders})", job_ids
            )
            return result

        return await self._connections.write(operation, commit=not dry_run)
