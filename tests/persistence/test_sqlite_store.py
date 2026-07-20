"""Integration coverage against the actual Phase 3 SQLite schema."""

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from revio.adapters.persistence.sqlite.schema import ACTIVE_JOB_INSERT_SQL, LEASE_SQL
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.application.retention import retain_terminal_history
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.queue import IngressDisposition, InstallationStatus
from revio.errors import PersistenceUnavailableError, QueueCapacityError


def _database(path: Path) -> DatabaseSettings:
    return DatabaseSettings(database_path=path)


def _pull(delivery: str, *, head: str = "head", action: str = "opened"):
    return normalize_webhook(
        "pull_request",
        delivery,
        {
            "action": action,
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {
                "number": 3,
                "base": {"sha": "base"},
                "head": {"sha": head},
            },
        },
    )


async def _persist(
    store: SQLiteStore,
    delivery: str,
    payload_hash: str,
    *,
    head: str = "head",
    action: str = "opened",
):
    return await store.persist(
        provider_id="github",
        delivery_identity=f"github:{delivery}",
        event_name="pull_request",
        payload_sha256=payload_hash,
        normalization=_pull(delivery, head=head, action=action),
        received_at=datetime.now(UTC),
    )


def test_conflict_safe_sql_matches_migrated_partial_index() -> None:
    assert "ON CONFLICT(provider_id, job_type, semantic_identity)" in ACTIVE_JOB_INSERT_SQL
    assert "WHERE state IN ('pending', 'running', 'retry_wait')" in ACTIVE_JOB_INSERT_SQL
    assert "DO NOTHING RETURNING id" in ACTIVE_JOB_INSERT_SQL
    assert "UPDATE queue_jobs" in LEASE_SQL and "RETURNING *" in LEASE_SQL


@pytest.mark.asyncio
async def test_delivery_and_semantic_idempotency_and_hash_conflict(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    first = await _persist(store, "one", "a" * 64)
    duplicate = await _persist(store, "one", "a" * 64)
    conflict = await _persist(store, "one", "b" * 64)
    semantic = await _persist(store, "two", "c" * 64, action="synchronize")
    assert first.disposition == IngressDisposition.ACCEPTED
    assert duplicate.disposition == IngressDisposition.IDEMPOTENT
    assert duplicate.job_id == first.job_id
    assert conflict.disposition == IngressDisposition.CONFLICT
    assert semantic.job_id == first.job_id
    status = await store.status()
    assert status["pending"] == status["active_jobs"] == 1
    assert status["terminal_jobs"] == 0
    assert status["deliveries"] == 2 and status["tombstones"] == 0
    assert status["database_bytes"] > 0


@pytest.mark.asyncio
async def test_concurrent_delivery_and_lease_are_single_winner(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    receipts = await asyncio.gather(
        _persist(store, "same", "a" * 64), _persist(store, "same", "a" * 64)
    )
    assert {receipt.disposition for receipt in receipts} == {
        IngressDisposition.ACCEPTED,
        IngressDisposition.IDEMPOTENT,
    }
    now = datetime.now(UTC)
    leases = await asyncio.gather(
        store.lease_next("worker-a", now), store.lease_next("worker-b", now)
    )
    assert sum(lease is not None for lease in leases) == 1


@pytest.mark.asyncio
async def test_capacity_rolls_back_new_delivery_but_accepts_duplicate(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"), QueueSettings(queue_max_active_jobs=1))
    await store.initialize()
    first = await _persist(store, "one", "a" * 64)
    with pytest.raises(QueueCapacityError):
        await _persist(store, "two", "b" * 64, head="other")
    duplicate = await _persist(store, "one", "a" * 64)
    assert duplicate.job_id == first.job_id
    assert (await store.status())["deliveries"] == 1


@pytest.mark.asyncio
async def test_terminal_timestamp_releases_semantic_identity(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    first = await _persist(store, "one", "a" * 64)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    assert await store.complete(lease, head_sha="head", base_sha="base", now=datetime.now(UTC))
    completed = await store.get_job(first.job_id or "")
    assert completed is not None and completed.terminal_at is not None
    later = await _persist(store, "two", "b" * 64, action="synchronize")
    assert later.job_id != first.job_id


@pytest.mark.asyncio
async def test_expired_lease_closes_attempt_and_is_recovered_once(tmp_path: Path) -> None:
    settings = QueueSettings(queue_lease_seconds=10, queue_heartbeat_seconds=4)
    store = SQLiteStore(_database(tmp_path / "revio.db"), settings)
    await store.initialize()
    await _persist(store, "one", "a" * 64)
    now = datetime.now(UTC)
    first = await store.lease_next("worker-before-restart", now)
    assert first is not None and first.attempt_number == 1
    recovered = await store.lease_next("worker-after-restart", now + timedelta(seconds=11))
    assert recovered is not None and recovered.attempt_number == 2
    assert not await store.complete(
        first, head_sha="head", base_sha="base", now=now + timedelta(seconds=11)
    )
    async with store.connect() as connection:
        attempts = list(
            await connection.execute_fetchall(
                "SELECT attempt_number, outcome FROM job_attempts ORDER BY attempt_number"
            )
        )
    assert [(row[0], row[1]) for row in attempts] == [(1, "lease_expired"), (2, None)]


@pytest.mark.asyncio
async def test_busy_retry_budget_is_bounded_and_consumes_no_job_attempt(tmp_path: Path) -> None:
    path = tmp_path / "revio.db"
    store = SQLiteStore(
        DatabaseSettings(
            database_path=path,
            database_busy_timeout_ms=10,
            database_busy_max_attempts=2,
            database_busy_max_elapsed_seconds=0.2,
        )
    )
    await store.initialize()
    lock = sqlite3.connect(path)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(PersistenceUnavailableError):
            await _persist(store, "busy", "a" * 64)
    finally:
        lock.rollback()
        lock.close()
    assert time.monotonic() - started < 0.5
    status = await store.status()
    assert status["deliveries"] == status["active_jobs"] == 0


@pytest.mark.asyncio
async def test_lifecycle_ordering_and_equal_timestamp_are_deterministic(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()

    async def lifecycle(delivery: str, action: str, updated_at: datetime) -> None:
        normalized = normalize_webhook(
            "installation",
            delivery,
            {"action": action, "installation": {"id": 9, "updated_at": updated_at.isoformat()}},
        )
        await store.persist(
            provider_id="github",
            delivery_identity=f"github:{delivery}",
            event_name="installation",
            payload_sha256=delivery * 8,
            normalization=normalized,
            received_at=datetime.now(UTC),
        )

    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(seconds=1)
    await lifecycle("a" * 8, "suspend", older)
    await lifecycle("b" * 8, "unsuspend", newer)
    await lifecycle("z" * 8, "suspend", older)
    state = await store.installation_state("github", "9")
    assert state is not None and state.state == InstallationStatus.ACTIVE
    await lifecycle("c" * 8, "deleted", newer)
    equal = await store.installation_state("github", "9")
    assert equal is not None and equal.state == InstallationStatus.DELETED
    await lifecycle("d" * 8, "created", newer + timedelta(seconds=1))
    reactivated = await store.installation_state("github", "9")
    assert reactivated is not None and reactivated.state == InstallationStatus.ACTIVE


@pytest.mark.asyncio
async def test_retention_creates_tombstone_and_overlap_fails_readiness(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    await _persist(store, "one", "a" * 64)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    terminal = datetime.now(UTC) - timedelta(days=3)
    await store.complete(lease, head_sha="head", base_sha="base", now=terminal)
    dry_run = await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=True)
    assert dry_run.jobs == 1 and (await store.status())["completed"] == 1
    result = await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=False)
    assert result.jobs == 1 and result.deliveries == 1
    duplicate = await _persist(store, "one", "a" * 64)
    conflict = await _persist(store, "one", "b" * 64)
    assert duplicate.disposition == IngressDisposition.IDEMPOTENT
    assert conflict.disposition == IngressDisposition.CONFLICT

    await _persist(store, "live", "c" * 64, head="live")
    async with store.connect() as connection:
        await connection.execute(
            "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, ?)",
            ("github", "github:live", "c" * 64, datetime.now(UTC).isoformat()),
        )
        await connection.commit()
    assert not await store.check_ready()
