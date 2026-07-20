"""Integration coverage against the actual Phase 3 SQLite schema."""

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from revio.adapters.observability import InMemoryQueueMetrics
from revio.adapters.persistence.sqlite.schema import ACTIVE_JOB_INSERT_SQL, LEASE_SQL
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.application.retention import retain_terminal_history
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.events import WebhookNormalizationResult
from revio.domain.queue import IngressDisposition, InstallationStatus
from revio.errors import (
    PersistenceUnavailableError,
    QueueCapacityError,
    RetentionIntegrityError,
)


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
    assert status["main_database_bytes"] > 0
    assert status["wal_bytes"] >= 0 and status["shm_bytes"] >= 0
    assert status["oldest_pending_age_seconds"] >= 0


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
async def test_concurrent_same_delivery_different_hash_is_one_conflict(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    receipts = await asyncio.gather(
        _persist(store, "same", "a" * 64), _persist(store, "same", "b" * 64)
    )
    assert {receipt.disposition for receipt in receipts} == {
        IngressDisposition.ACCEPTED,
        IngressDisposition.CONFLICT,
    }
    assert (await store.status())["deliveries"] == 1
    assert (await store.status())["active_jobs"] == 1


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
async def test_final_expired_attempt_and_impossible_exhausted_states_become_dead(
    tmp_path: Path,
) -> None:
    queue = QueueSettings(queue_lease_seconds=10, queue_heartbeat_seconds=4, queue_max_attempts=1)
    store = SQLiteStore(_database(tmp_path / "revio.db"), queue)
    await store.initialize()
    first = await _persist(store, "running", "a" * 64, head="running")
    now = datetime.now(UTC)
    assert await store.lease_next("worker", now) is not None
    await _persist(store, "pending", "b" * 64, head="pending")
    await _persist(store, "retry", "c" * 64, head="retry")
    async with store.connect() as connection:
        await connection.execute(
            "UPDATE queue_jobs SET attempt_count=max_attempts "
            "WHERE semantic_identity LIKE '%pending%'"
        )
        await connection.execute(
            "UPDATE queue_jobs SET state='retry_wait', attempt_count=max_attempts "
            "WHERE semantic_identity LIKE '%retry%'"
        )
        await connection.commit()
    assert await store.lease_next("recovery", now + timedelta(seconds=11)) is None
    async with store.connect() as connection:
        states = list(
            await connection.execute_fetchall(
                "SELECT id, state, terminal_reason FROM queue_jobs ORDER BY id"
            )
        )
        attempt = next(
            iter(
                await connection.execute_fetchall(
                    "SELECT finished_at, outcome FROM job_attempts WHERE job_id=?",
                    (first.job_id,),
                )
            )
        )
    assert all(row["state"] == "dead" for row in states)
    assert attempt["finished_at"] is not None and attempt["outcome"] == "lease_expired"


@pytest.mark.asyncio
async def test_busy_retry_budget_is_bounded_and_consumes_no_job_attempt(tmp_path: Path) -> None:
    path = tmp_path / "revio.db"
    acquisitions = 0

    def count(stage: str) -> None:
        nonlocal acquisitions
        if stage == "before_begin":
            acquisitions += 1

    store = SQLiteStore(
        DatabaseSettings(
            database_path=path,
            database_busy_timeout_ms=10,
            database_busy_max_attempts=2,
            database_busy_max_elapsed_seconds=0.2,
        ),
        failure_hook=count,
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
    assert acquisitions == 2
    status = await store.status()
    assert status["deliveries"] == status["active_jobs"] == 0


@pytest.mark.asyncio
async def test_injected_busy_exhaustion_is_bounded_before_transaction(tmp_path: Path) -> None:
    attempts = 0

    def busy(stage: str) -> None:
        nonlocal attempts
        if stage == "before_begin":
            attempts += 1
            raise aiosqlite.OperationalError("database is locked")

    store = SQLiteStore(
        DatabaseSettings(
            database_path=tmp_path / "revio.db",
            database_busy_max_attempts=3,
            database_busy_max_elapsed_seconds=0.01,
        ),
        failure_hook=busy,
    )
    await store.initialize()
    with pytest.raises(PersistenceUnavailableError):
        await _persist(store, "busy", "a" * 64)
    assert attempts == 1
    assert (await store.status())["deliveries"] == 0


@pytest.mark.asyncio
async def test_lifecycle_uses_serialized_arrival_not_provider_timestamp(tmp_path: Path) -> None:
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
    assert state is not None and state.state == InstallationStatus.SUSPENDED
    await lifecycle("c" * 8, "deleted", newer)
    equal = await store.installation_state("github", "9")
    assert equal is not None and equal.state == InstallationStatus.DELETED
    await lifecycle("d" * 8, "created", newer + timedelta(seconds=1))
    reactivated = await store.installation_state("github", "9")
    assert reactivated is not None and reactivated.state == InstallationStatus.ACTIVE


@pytest.mark.asyncio
async def test_lifecycle_missing_mixed_timestamps_and_duplicate_follow_arrival(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()

    async def lifecycle(
        delivery: str, action: str, updated_at: datetime | None
    ) -> IngressDisposition:
        installation: dict[str, object] = {"id": 9}
        if updated_at is not None:
            installation["updated_at"] = updated_at.isoformat()
        normalized = normalize_webhook(
            "installation", delivery, {"action": action, "installation": installation}
        )
        receipt = await store.persist(
            provider_id="github",
            delivery_identity=f"github:{delivery}",
            event_name="installation",
            payload_sha256=(delivery + "0" * 64)[:64],
            normalization=normalized,
            received_at=datetime.now(UTC),
        )
        return receipt.disposition

    now = datetime.now(UTC)
    assert await lifecycle("suspend", "suspend", now) == IngressDisposition.ACCEPTED
    assert await lifecycle("unsuspend", "unsuspend", None) == IngressDisposition.ACCEPTED
    assert (
        await lifecycle("delete", "deleted", now - timedelta(days=1)) == IngressDisposition.ACCEPTED
    )
    assert (
        await lifecycle("delete", "deleted", now - timedelta(days=1))
        == IngressDisposition.IDEMPOTENT
    )
    state = await store.installation_state("github", "9")
    assert state is not None and state.state == InstallationStatus.DELETED


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_json", "schema_version"),
    [("{", 1), ("{}", 1), (None, 2)],
)
async def test_invalid_candidate_is_dead_and_next_valid_job_is_leased(
    tmp_path: Path, event_json: str | None, schema_version: int
) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    invalid = await _persist(store, "invalid", "a" * 64, head="invalid")
    valid = await _persist(store, "valid", "b" * 64, head="valid")
    async with store.connect() as connection:
        values: tuple[object, ...]
        if event_json is None:
            values = (schema_version, invalid.job_id)
            await connection.execute(
                "UPDATE queue_jobs SET event_schema_version=? WHERE id=?", values
            )
        else:
            values = (event_json, schema_version, invalid.job_id)
            await connection.execute(
                "UPDATE queue_jobs SET event_json=?, event_schema_version=? WHERE id=?", values
            )
        await connection.execute(
            "UPDATE queue_jobs SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (invalid.job_id,),
        )
        await connection.commit()
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None and lease.job.id == valid.job_id
    async with store.connect() as connection:
        invalid_row = next(
            iter(
                await connection.execute_fetchall(
                    "SELECT state, terminal_reason, terminal_at, lease_owner, "
                    "lease_expires_at, safe_error_class, safe_error_message "
                    "FROM queue_jobs WHERE id=?",
                    (invalid.job_id,),
                )
            )
        )
        invalid_attempts = list(
            await connection.execute_fetchall(
                "SELECT * FROM job_attempts WHERE job_id=?", (invalid.job_id,)
            )
        )
    assert invalid_row["state"] == "dead"
    assert invalid_row["terminal_reason"] == "invalid_job_schema"
    assert invalid_row["terminal_at"] is not None
    assert invalid_row["lease_owner"] is invalid_row["lease_expires_at"] is None
    assert invalid_row["safe_error_class"] == "InvalidJobError"
    assert invalid_row["safe_error_message"] == "persisted job schema is invalid"
    assert invalid_attempts == []
    assert await store.lease_next("restart", datetime.now(UTC)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_tombstone_lookup",
        "after_delivery_insert",
        "after_semantic_job_lookup",
        "after_capacity_count",
        "after_job_insert",
        "after_delivery_job_link",
        "before_commit",
    ],
)
async def test_ingress_failure_stages_roll_back_atomically(tmp_path: Path, stage: str) -> None:
    enabled = True

    def fail(current: str) -> None:
        if enabled and current == stage:
            raise aiosqlite.OperationalError("injected database failure")

    store = SQLiteStore(_database(tmp_path / "revio.db"), failure_hook=fail)
    await store.initialize()
    with pytest.raises(PersistenceUnavailableError):
        await _persist(store, "failure", "a" * 64)
    assert (await store.status())["deliveries"] == 0
    assert (await store.status())["active_jobs"] == 0
    enabled = False
    assert (await _persist(store, "reused", "b" * 64)).disposition == "accepted"


@pytest.mark.asyncio
async def test_lifecycle_failure_rolls_back_delivery_and_state(tmp_path: Path) -> None:
    def fail(stage: str) -> None:
        if stage == "after_lifecycle_upsert":
            raise aiosqlite.OperationalError("injected database failure")

    store = SQLiteStore(_database(tmp_path / "revio.db"), failure_hook=fail)
    await store.initialize()
    normalization = normalize_webhook(
        "installation", "lifecycle", {"action": "suspend", "installation": {"id": 9}}
    )
    with pytest.raises(PersistenceUnavailableError):
        await store.persist(
            provider_id="github",
            delivery_identity="github:lifecycle",
            event_name="installation",
            payload_sha256="a" * 64,
            normalization=normalization,
            received_at=datetime.now(UTC),
        )
    assert await store.installation_state("github", "9") is None
    assert (await store.status())["deliveries"] == 0


@pytest.mark.asyncio
async def test_attempt_insert_failure_rolls_back_lease(tmp_path: Path) -> None:
    enabled = False

    def fail(stage: str) -> None:
        if enabled and stage == "after_attempt_insert":
            raise aiosqlite.OperationalError("injected database failure")

    store = SQLiteStore(_database(tmp_path / "revio.db"), failure_hook=fail)
    await store.initialize()
    receipt = await _persist(store, "attempt", "a" * 64)
    enabled = True
    with pytest.raises(PersistenceUnavailableError):
        await store.lease_next("worker", datetime.now(UTC))
    async with store.connect() as connection:
        row = next(
            iter(
                await connection.execute_fetchall(
                    "SELECT state, attempt_count FROM queue_jobs WHERE id=?", (receipt.job_id,)
                )
            )
        )
        attempts = list(await connection.execute_fetchall("SELECT * FROM job_attempts"))
    assert tuple(row) == ("pending", 0)
    assert attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_begin", "after_begin", "after_delivery_insert"])
async def test_cancellation_before_commit_rolls_back_and_connection_reuses(
    tmp_path: Path, stage: str
) -> None:
    enabled = True

    def cancel(current: str) -> None:
        if enabled and current == stage:
            raise asyncio.CancelledError

    store = SQLiteStore(_database(tmp_path / "revio.db"), failure_hook=cancel)
    await store.initialize()
    with pytest.raises(asyncio.CancelledError):
        await _persist(store, "cancelled", "a" * 64)
    assert (await store.status())["deliveries"] == 0
    enabled = False
    await _persist(store, "after-cancel", "b" * 64)
    assert (await store.status())["deliveries"] == 1


@pytest.mark.asyncio
async def test_cancellation_during_delayed_commit_resolves_known_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = aiosqlite.Connection.commit

    async def delayed_commit(connection: aiosqlite.Connection) -> None:
        commit_started.set()
        await release_commit.wait()
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", delayed_commit)
    task = asyncio.create_task(_persist(store, "commit", "a" * 64))
    await commit_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.status())["deliveries"] == 1


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_and_connection_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    original_commit = aiosqlite.Connection.commit
    fail_next = True

    async def failing_commit(connection: aiosqlite.Connection) -> None:
        nonlocal fail_next
        if fail_next:
            fail_next = False
            raise aiosqlite.OperationalError("injected commit failure")
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", failing_commit)
    with pytest.raises(PersistenceUnavailableError):
        await _persist(store, "failed-commit", "a" * 64)
    assert (await store.status())["deliveries"] == 0
    assert (await _persist(store, "successful-commit", "b" * 64)).job_id is not None


@pytest.mark.asyncio
async def test_concurrent_semantic_and_capacity_boundaries(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"), QueueSettings(queue_max_active_jobs=1))
    await store.initialize()
    semantic = await asyncio.gather(
        _persist(store, "semantic-a", "a" * 64),
        _persist(store, "semantic-b", "b" * 64, action="synchronize"),
    )
    assert len({receipt.job_id for receipt in semantic}) == 1
    assert (await store.status())["deliveries"] == 2

    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    assert await store.complete(lease, head_sha="head", base_sha="base", now=datetime.now(UTC))

    other = await asyncio.gather(
        _persist(store, "capacity-a", "c" * 64, head="other-a"),
        _persist(store, "capacity-b", "d" * 64, head="other-b"),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in other) == 1
    assert sum(isinstance(result, QueueCapacityError) for result in other) == 1
    assert (await store.status())["deliveries"] == 3


@pytest.mark.asyncio
async def test_retention_matching_tombstone_and_conflict_batch_rollback(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    first = await _persist(store, "first", "a" * 64, head="first")
    first_lease = await store.lease_next("worker", datetime.now(UTC))
    assert first_lease is not None
    old = datetime.now(UTC) - timedelta(days=3)
    assert await store.complete(first_lease, head_sha="first", base_sha="base", now=old)
    second = await _persist(store, "second", "b" * 64, head="second")
    second_lease = await store.lease_next("worker", datetime.now(UTC))
    assert second_lease is not None
    assert await store.complete(second_lease, head_sha="second", base_sha="base", now=old)
    async with store.connect() as connection:
        await connection.execute(
            "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, ?)",
            ("github", "github:first", "a" * 64, datetime.now(UTC).isoformat()),
        )
        await connection.execute(
            "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, ?)",
            ("github", "github:second", "x" * 64, datetime.now(UTC).isoformat()),
        )
        await connection.commit()
    with pytest.raises(RetentionIntegrityError) as caught:
        await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=False)
    assert "github" not in str(caught.value)
    async with store.connect() as connection:
        jobs = list(await connection.execute_fetchall("SELECT id FROM queue_jobs"))
        deliveries = list(await connection.execute_fetchall("SELECT id FROM webhook_deliveries"))
        attempts = list(await connection.execute_fetchall("SELECT id FROM job_attempts"))
    assert {row["id"] for row in jobs} == {first.job_id, second.job_id}
    assert len(deliveries) == len(attempts) == 2


@pytest.mark.asyncio
async def test_retention_accepts_matching_existing_tombstone_and_replay(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    await _persist(store, "matching", "a" * 64)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    old = datetime.now(UTC) - timedelta(days=3)
    assert await store.complete(lease, head_sha="head", base_sha="base", now=old)
    async with store.connect() as connection:
        await connection.execute(
            "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, ?)",
            ("github", "github:matching", "a" * 64, datetime.now(UTC).isoformat()),
        )
        await connection.commit()
    result = await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=False)
    assert result.jobs == result.attempts == result.deliveries == 1
    replay = await _persist(store, "matching", "a" * 64)
    assert replay.disposition == IngressDisposition.IDEMPOTENT
    assert await store.check_ready()


@pytest.mark.asyncio
async def test_retention_preserves_active_ignored_and_lifecycle_rows(tmp_path: Path) -> None:
    store = SQLiteStore(_database(tmp_path / "revio.db"))
    await store.initialize()
    terminal = await _persist(store, "terminal", "a" * 64, head="terminal")
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None and lease.job.id == terminal.job_id
    old = datetime.now(UTC) - timedelta(days=3)
    assert await store.complete(lease, head_sha="terminal", base_sha="base", now=old)
    await _persist(store, "active", "b" * 64, head="active")
    await store.persist(
        provider_id="github",
        delivery_identity="github:ignored",
        event_name="push",
        payload_sha256="c" * 64,
        normalization=WebhookNormalizationResult(disposition="ignored", reason="unsupported event"),
        received_at=datetime.now(UTC),
    )
    lifecycle = normalize_webhook(
        "installation", "lifecycle", {"action": "suspend", "installation": {"id": 9}}
    )
    await store.persist(
        provider_id="github",
        delivery_identity="github:lifecycle",
        event_name="installation",
        payload_sha256="d" * 64,
        normalization=lifecycle,
        received_at=datetime.now(UTC),
    )
    result = await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=False)
    assert result.jobs == result.attempts == result.deliveries == 1
    status = await store.status()
    assert status["active_jobs"] == 1 and status["deliveries"] == 3
    state = await store.installation_state("github", "9")
    assert state is not None and state.state == InstallationStatus.SUSPENDED


@pytest.mark.asyncio
async def test_bounded_phase3_metrics_are_emitted_without_identity_labels(
    tmp_path: Path,
) -> None:
    metrics = InMemoryQueueMetrics()
    store = SQLiteStore(
        _database(tmp_path / "revio.db"),
        QueueSettings(queue_max_active_jobs=1),
        metrics=metrics,
    )
    await store.initialize()
    first = await _persist(store, "metrics", "a" * 64)
    await _persist(store, "metrics", "a" * 64)
    await _persist(store, "semantic", "b" * 64, action="synchronize")
    with pytest.raises(QueueCapacityError):
        await _persist(store, "capacity", "c" * 64, head="other")
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None and lease.job.id == first.job_id
    assert await store.complete(lease, head_sha="head", base_sha="base", now=datetime.now(UTC))
    assert metrics.counters[("delivery_accepted", "accepted")] == 2
    assert metrics.counters[("delivery_exact_duplicate", None)] == 1
    assert metrics.counters[("semantic_duplicate", None)] == 1
    assert metrics.counters[("capacity_rejection", None)] == 1
    assert metrics.counters[("job_created", None)] == 1
    assert metrics.counters[("lease_acquired", None)] == 1
    assert metrics.counters[("completed", None)] == 1
    assert all("github" not in metric and "metrics" not in metric for metric, _ in metrics.counters)
