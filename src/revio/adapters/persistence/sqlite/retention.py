"""Atomic terminal-history retention and permanent delivery tombstones."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import timestamp
from revio.domain.lifecycle import FindingRetentionResult
from revio.domain.queue import RetentionResult
from revio.errors import (
    PersistenceIntegrityError,
    PersistenceNotCommittedError,
    PersistenceUnavailableError,
    RetentionIntegrityError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RetentionBatch:
    result: RetentionResult
    job_ids: tuple[str, ...]
    delivery_facts: tuple[tuple[str, str, str], ...]


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

        async def operation(connection: aiosqlite.Connection) -> _RetentionBatch:
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
                return _RetentionBatch(
                    result=RetentionResult(jobs=0, attempts=0, deliveries=0),
                    job_ids=(),
                    delivery_facts=(),
                )
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
                return _RetentionBatch(
                    result=result,
                    job_ids=tuple(job_ids),
                    delivery_facts=tuple(
                        (row["provider_id"], row["delivery_identity"], row["payload_sha256"])
                        for row in deliveries
                    ),
                )

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
            return _RetentionBatch(
                result=result,
                job_ids=tuple(job_ids),
                delivery_facts=tuple(
                    (row["provider_id"], row["delivery_identity"], row["payload_sha256"])
                    for row in deliveries
                ),
            )

        async def reconcile(expected: _RetentionBatch) -> _RetentionBatch:
            async def read(connection: aiosqlite.Connection) -> _RetentionBatch:
                if not expected.job_ids:
                    return expected
                placeholders = ",".join("?" for _ in expected.job_ids)
                jobs = list(
                    await connection.execute_fetchall(
                        f"SELECT id FROM queue_jobs WHERE id IN ({placeholders})",
                        expected.job_ids,
                    )
                )
                attempts = list(
                    await connection.execute_fetchall(
                        f"SELECT 1 FROM job_attempts WHERE job_id IN ({placeholders}) LIMIT 1",
                        expected.job_ids,
                    )
                )
                live = list(
                    await connection.execute_fetchall(
                        f"SELECT 1 FROM webhook_deliveries WHERE linked_job_id IN ({placeholders})",
                        expected.job_ids,
                    )
                )
                tombstones_confirmed = 0
                for provider_id, delivery_identity, payload_hash in expected.delivery_facts:
                    rows = list(
                        await connection.execute_fetchall(
                            "SELECT payload_sha256 FROM webhook_delivery_tombstones "
                            "WHERE provider_id=? AND delivery_identity=?",
                            (provider_id, delivery_identity),
                        )
                    )
                    if rows and rows[0]["payload_sha256"] == payload_hash:
                        tombstones_confirmed += 1
                    elif rows:
                        raise PersistenceIntegrityError(
                            "retention commit reconciliation found inconsistent state"
                        )
                if (
                    not jobs
                    and not attempts
                    and not live
                    and tombstones_confirmed == len(expected.delivery_facts)
                ):
                    return expected
                if len(jobs) == len(expected.job_ids) and len(live) == len(expected.delivery_facts):
                    raise PersistenceNotCommittedError("retention commit was not confirmed")
                raise PersistenceIntegrityError(
                    "retention commit reconciliation found inconsistent state"
                )

            return await self._connections.read(read)

        batch = await self._connections.write(
            operation,
            commit=not dry_run,
            reconcile=None if dry_run else reconcile,
        )
        return batch.result


class SQLiteFindingRetentionRepository:
    """Phase 6 lifecycle pruning of confirmed-closed finding history.

    Only ``resolved`` findings whose resolution is older than the cutoff are eligible. Open and
    resolution_indeterminate findings, and any unresolved discovery/resolution operation, are
    preserved indefinitely. Deletion cascades occurrences, links, revalidation results, and the
    resolved operation atomically with the eligible finding.
    """

    def __init__(
        self,
        connections: SQLiteConnectionPolicy,
        capability_check: Callable[[], Awaitable[bool]],
    ) -> None:
        self._connections = connections
        self._capability_check = capability_check

    async def retain_closed_findings(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        dry_run: bool,
        correlation_id: str,
    ) -> FindingRetentionResult:
        if not await self._capability_check():
            raise PersistenceUnavailableError("database is not ready for retention") from None

        async def operation(connection: aiosqlite.Connection) -> FindingRetentionResult:
            rows = list(
                await connection.execute_fetchall(
                    "SELECT id FROM finding_records WHERE state='resolved' "
                    "AND resolved_at IS NOT NULL AND resolved_at<? "
                    "ORDER BY resolved_at, id LIMIT ?",
                    (timestamp(cutoff), batch_size),
                )
            )
            finding_ids = [row["id"] for row in rows]
            if not finding_ids:
                return FindingRetentionResult(
                    findings=0, occurrences=0, links=0, operations=0, results=0
                )
            placeholders = ",".join("?" for _ in finding_ids)

            def _count(table: str) -> str:
                return (
                    f"SELECT COUNT(*) AS total FROM {table} "
                    f"WHERE finding_record_id IN ({placeholders})"
                )

            occurrences = next(
                iter(await connection.execute_fetchall(_count("finding_occurrences"), finding_ids))
            )["total"]
            links = next(
                iter(await connection.execute_fetchall(_count("review_thread_links"), finding_ids))
            )["total"]
            operations = next(
                iter(
                    await connection.execute_fetchall(
                        _count("thread_resolution_operations"), finding_ids
                    )
                )
            )["total"]
            results = next(
                iter(
                    await connection.execute_fetchall(
                        _count("finding_revalidation_results"), finding_ids
                    )
                )
            )["total"]
            result = FindingRetentionResult(
                findings=len(finding_ids),
                occurrences=occurrences,
                links=links,
                operations=operations,
                results=results,
            )
            if dry_run:
                return result
            # Integrity guard: never delete a finding that still has an unresolved operation.
            unresolved = list(
                await connection.execute_fetchall(
                    f"SELECT 1 FROM thread_resolution_operations "
                    f"WHERE finding_record_id IN ({placeholders}) "
                    "AND state NOT IN ('completed','reconciled') LIMIT 1",
                    finding_ids,
                )
            )
            if unresolved:
                logger.error(
                    "finding_retention_integrity_conflict",
                    extra={"correlation_id": correlation_id},
                )
                self._connections.metric("finding_retention_integrity_conflict")
                raise RetentionIntegrityError(
                    "closed-finding retention found an unresolved operation"
                ) from None
            # ON DELETE CASCADE removes occurrences, links, results, and the resolved operation.
            await connection.execute(
                f"DELETE FROM finding_records WHERE id IN ({placeholders})", finding_ids
            )
            return result

        async def reconcile(expected: FindingRetentionResult) -> FindingRetentionResult:
            async def read(connection: aiosqlite.Connection) -> FindingRetentionResult:
                return expected

            return await self._connections.read(read)

        return await self._connections.write(
            operation,
            commit=not dry_run,
            reconcile=None if dry_run else reconcile,
        )
