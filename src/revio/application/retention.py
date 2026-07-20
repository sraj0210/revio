"""Offline terminal-history retention with permanent delivery tombstones."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from revio.adapters.persistence.sqlite.store import SQLiteStore, timestamp


@dataclass(frozen=True)
class RetentionResult:
    jobs: int
    attempts: int
    deliveries: int


async def retain_terminal_history(
    store: SQLiteStore, *, age_days: int, batch_size: int, dry_run: bool
) -> RetentionResult:
    """Count or delete an eligible bounded batch; caller holds exclusive lock."""
    cutoff = datetime.now(UTC) - timedelta(days=age_days)
    async with store.connect() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        try:
            jobs = await connection.execute_fetchall(
                "SELECT id FROM queue_jobs "
                "WHERE state IN ('completed','dead','cancelled','superseded') "
                "AND terminal_at < ? ORDER BY terminal_at LIMIT ?",
                (timestamp(cutoff), batch_size),
            )
            jobs = list(jobs)
            job_ids = [row["id"] for row in jobs]
            attempts = 0
            deliveries = 0
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                counted = await connection.execute_fetchall(
                    f"SELECT COUNT(*) AS total FROM job_attempts WHERE job_id IN ({placeholders})",
                    job_ids,
                )
                counted = list(counted)
                attempts = counted[0]["total"]
                eligible = await connection.execute_fetchall(
                    "SELECT d.* FROM webhook_deliveries d "
                    f"WHERE d.linked_job_id IN ({placeholders})",
                    job_ids,
                )
                eligible = list(eligible)
                deliveries = len(eligible)
                if not dry_run:
                    now = timestamp(datetime.now(UTC))
                    for row in eligible:
                        await connection.execute(
                            "INSERT INTO webhook_delivery_tombstones "
                            "(provider_id, delivery_identity, payload_sha256, retained_at) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(provider_id, delivery_identity) "
                            "DO NOTHING",
                            (
                                row["provider_id"],
                                row["delivery_identity"],
                                row["payload_sha256"],
                                now,
                            ),
                        )
                    for row in eligible:
                        await connection.execute(
                            "DELETE FROM webhook_deliveries WHERE id=?", (row["id"],)
                        )
                    await connection.execute(
                        f"DELETE FROM queue_jobs WHERE id IN ({placeholders})", job_ids
                    )
            if dry_run:
                await connection.rollback()
            else:
                await connection.commit()
            return RetentionResult(len(jobs), attempts, deliveries)
        except BaseException:
            await connection.rollback()
            raise
