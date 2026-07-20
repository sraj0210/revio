"""Alembic-backed regressions for the final Phase 3 durability remediation."""

import asyncio
import hashlib
import hmac
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from revio.adapters.persistence.sqlite.connection import FailureHook
from revio.adapters.persistence.sqlite.queue import INVALID_JOB_CLEANUP_BATCH_SIZE
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.api.app import create_app
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.application.retention import retain_terminal_history
from revio.cli import queue_status
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.domain.capabilities import SCMCapabilities
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef
from revio.domain.models import ChangeRequest, DiffCollection
from revio.domain.queue import IngressDisposition, JobLease
from revio.errors import PersistenceUnavailableError, QueueCapacityError, RetentionIntegrityError
from revio.registries import ProviderRegistry, SCMAdapterBundle


class MigratedDatabase(Protocol):
    path: Path

    def store(
        self,
        queue: QueueSettings | None = None,
        *,
        failure_hook: FailureHook | None = None,
    ) -> SQLiteStore: ...


@dataclass
class _Cache:
    invalidated: list[InstallationRef] = field(default_factory=lambda: list[InstallationRef]())

    async def invalidate(self, installation: InstallationRef) -> None:
        self.invalidated.append(installation)


class _Reader:
    def __init__(self, *, block: bool = False) -> None:
        self.started = asyncio.Event()
        self.returned = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.reads = 0

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        self.reads += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        self.returned.set()
        return ChangeRequest(
            target=target,
            title="title",
            base_sha="base",
            head_sha="head",
            state="open",
        )

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        raise AssertionError("Phase 3 must not fetch diffs")


@dataclass
class _Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current


def _processor(
    store: SQLiteStore,
    queue: QueueSettings,
    reader: _Reader,
    *,
    clock: _Clock | None = None,
) -> QueueProcessor:
    providers = ProviderRegistry()
    providers.register_scm(
        GITHUB_PROVIDER_ID,
        SCMAdapterBundle(reader=reader, capabilities=SCMCapabilities()),
    )
    return QueueProcessor(store, providers, _Cache(), queue, clock=clock)


def _pull(delivery: str, *, head: str = "head"):
    return normalize_webhook(
        "pull_request",
        delivery,
        {
            "action": "opened",
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
):
    return await store.persist(
        provider_id="github",
        delivery_identity=f"github:{delivery}",
        event_name="pull_request",
        payload_sha256=payload_hash,
        normalization=_pull(delivery, head=head),
        received_at=datetime.now(UTC),
    )


async def _insert_invalid_jobs(store: SQLiteStore, count: int) -> None:
    now = datetime.now(UTC).isoformat()
    rows = [
        (
            f"invalid-{index}",
            "github",
            "change_request_validation",
            f"invalid:{index}",
            "{",
            3,
            now,
            f"2000-01-01T00:00:{index % 60:02d}+00:00",
            now,
        )
        for index in range(count)
    ]
    async with store.connect() as connection:
        await connection.executemany(
            "INSERT INTO queue_jobs "
            "(id,provider_id,job_type,semantic_identity,event_json,state,attempt_count,"
            "max_attempts,available_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'pending',0,?,?,?,?)",
            rows,
        )
        await connection.commit()


@pytest.mark.asyncio
async def test_commit_success_then_exception_reconciles_accepted_http_result(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    store = alembic_database.store()
    original_commit = aiosqlite.Connection.commit
    committed_connection: aiosqlite.Connection | None = None
    raised = False

    async def commit_then_raise(connection: aiosqlite.Connection) -> None:
        nonlocal committed_connection, raised
        await original_commit(connection)
        if not raised:
            raised = True
            committed_connection = connection
            raise aiosqlite.OperationalError("post-commit sentinel")

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_then_raise)
    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
        github_webhook_mode="durable",
        github_webhook_secret=SecretStr("webhook-secret"),
    )
    app = create_app(settings, persistence=store)
    payload = {
        "action": "opened",
        "installation": {"id": 9},
        "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
        "pull_request": {
            "number": 3,
            "base": {"sha": "base"},
            "head": {"sha": "head"},
        },
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-github-delivery": "commit-certainty",
        "x-github-event": "pull_request",
        "x-hub-signature-256": f"sha256={signature}",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert committed_connection is not None
    with pytest.raises(ValueError, match="no active connection"):
        _ = committed_connection.in_transaction
    status = await store.status()
    assert status["deliveries"] == status["active_jobs"] == 1

    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    replay = await _persist(store, "commit-certainty", hashlib.sha256(body).hexdigest())
    assert replay.disposition == IngressDisposition.IDEMPOTENT
    assert (await store.status())["active_jobs"] == 1


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_and_never_claims_acceptance(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    store = alembic_database.store()
    original_commit = aiosqlite.Connection.commit
    failed = False

    async def fail_before_commit(connection: aiosqlite.Connection) -> None:
        nonlocal failed
        if not failed:
            failed = True
            assert connection.in_transaction
            raise aiosqlite.OperationalError("pre-commit sentinel")
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", fail_before_commit)
    settings = GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
        github_webhook_mode="durable",
        github_webhook_secret=SecretStr("webhook-secret"),
    )
    app = create_app(settings, persistence=store)
    body = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {
                "number": 3,
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            },
        }
    ).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-github-delivery": "absent",
        "x-github-event": "pull_request",
        "x-hub-signature-256": f"sha256={signature}",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "durable ingress unavailable"}
    assert (await store.status())["deliveries"] == 0
    assert (await store.status())["active_jobs"] == 0

    receipt = await _persist(store, "reused", "b" * 64)
    assert receipt.disposition == IngressDisposition.ACCEPTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_count",
    [INVALID_JOB_CLEANUP_BATCH_SIZE, INVALID_JOB_CLEANUP_BATCH_SIZE + 1, 1_001],
)
async def test_invalid_backlog_commits_bounded_batches_and_reaches_valid_job(
    alembic_database: MigratedDatabase,
    invalid_count: int,
) -> None:
    commits = 0

    def count_commits(stage: str) -> None:
        nonlocal commits
        if stage == "before_commit":
            commits += 1

    setup = alembic_database.store()
    await _insert_invalid_jobs(setup, invalid_count)
    valid = await _persist(setup, "valid", "f" * 64, head="valid")
    store = alembic_database.store(failure_hook=count_commits)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None and lease.job.id == valid.job_id
    assert commits == invalid_count // INVALID_JOB_CLEANUP_BATCH_SIZE + 1
    async with store.connect() as connection:
        invalid = list(
            await connection.execute_fetchall(
                "SELECT state, terminal_reason, terminal_at, lease_owner, lease_expires_at "
                "FROM queue_jobs WHERE id LIKE 'invalid-%'"
            )
        )
        invalid_attempts = list(
            await connection.execute_fetchall(
                "SELECT 1 FROM job_attempts WHERE job_id LIKE 'invalid-%'"
            )
        )
    assert len(invalid) == invalid_count
    assert all(
        row["state"] == "dead"
        and row["terminal_reason"] == "invalid_job_schema"
        and row["terminal_at"] is not None
        and row["lease_owner"] is None
        and row["lease_expires_at"] is None
        for row in invalid
    )
    assert invalid_attempts == []


@pytest.mark.asyncio
async def test_invalid_backlog_restart_continues_after_committed_partial_batch(
    alembic_database: MigratedDatabase,
) -> None:
    setup = alembic_database.store()
    await _insert_invalid_jobs(setup, INVALID_JOB_CLEANUP_BATCH_SIZE + 5)
    valid = await _persist(setup, "valid-restart", "e" * 64, head="valid-restart")
    begins = 0

    def stop_after_batch(stage: str) -> None:
        nonlocal begins
        if stage == "before_begin":
            begins += 1
            if begins == 2:
                raise PersistenceUnavailableError("bounded restart diagnostic")

    interrupted = alembic_database.store(failure_hook=stop_after_batch)
    with pytest.raises(PersistenceUnavailableError):
        await interrupted.lease_next("first-worker", datetime.now(UTC))
    async with setup.connect() as connection:
        dead = next(
            iter(
                await connection.execute_fetchall(
                    "SELECT COUNT(*) FROM queue_jobs WHERE id LIKE 'invalid-%' AND state='dead'"
                )
            )
        )[0]
    assert dead == INVALID_JOB_CLEANUP_BATCH_SIZE

    restarted = alembic_database.store()
    lease = await restarted.lease_next("second-worker", datetime.now(UTC))
    assert lease is not None and lease.job.id == valid.job_id


@pytest.mark.asyncio
async def test_worker_run_contains_malformed_backlog_and_processes_valid_job(
    alembic_database: MigratedDatabase,
) -> None:
    store = alembic_database.store()
    await _insert_invalid_jobs(store, 1_001)
    await _persist(store, "worker-valid", "c" * 64)
    reader = _Reader(block=True)
    queue = QueueSettings()
    worker = QueueWorker(store, _processor(store, queue, reader), queue, "worker")
    running = asyncio.create_task(worker.run())
    await asyncio.wait_for(reader.started.wait(), timeout=5)
    reader.release.set()
    await reader.returned.wait()
    for _ in range(20):
        if (await store.status()).get("completed") == 1:
            break
        await asyncio.sleep(0)
    worker.request_stop()
    await asyncio.wait_for(running, timeout=1)
    status = await store.status()
    assert status["dead"] == 1_001
    assert status["completed"] == 1


@pytest.mark.asyncio
async def test_semantic_duplicate_concurrency_uses_one_canonical_job(
    alembic_database: MigratedDatabase,
) -> None:
    first = alembic_database.store()
    second = alembic_database.store()
    start = asyncio.Event()

    async def contend(store: SQLiteStore, delivery: str, payload_hash: str):
        await start.wait()
        return await _persist(store, delivery, payload_hash)

    tasks = (
        asyncio.create_task(contend(first, "semantic-a", "a" * 64)),
        asyncio.create_task(contend(second, "semantic-b", "b" * 64)),
    )
    await asyncio.sleep(0)
    start.set()
    receipts = await asyncio.gather(*tasks)
    assert {receipt.disposition for receipt in receipts} == {IngressDisposition.ACCEPTED}
    assert receipts[0].job_id == receipts[1].job_id
    status = await first.status()
    assert status["deliveries"] == 2
    assert status["active_jobs"] == 1


@pytest.mark.asyncio
async def test_capacity_boundary_concurrency_never_exceeds_limit(
    alembic_database: MigratedDatabase,
) -> None:
    queue = QueueSettings(queue_max_active_jobs=1)
    first = alembic_database.store(queue)
    second = alembic_database.store(queue)
    start = asyncio.Event()

    async def contend(store: SQLiteStore, delivery: str, head: str):
        await start.wait()
        return await _persist(store, delivery, head[0] * 64, head=head)

    tasks = (
        asyncio.create_task(contend(first, "capacity-a", "alpha")),
        asyncio.create_task(contend(second, "capacity-b", "bravo")),
    )
    await asyncio.sleep(0)
    start.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, QueueCapacityError) for outcome in outcomes) == 1
    status = await first.status()
    assert status["active_jobs"] == 1
    assert status["deliveries"] == 1


@pytest.mark.asyncio
async def test_lease_concurrency_has_one_owner_and_one_attempt(
    alembic_database: MigratedDatabase,
) -> None:
    first = alembic_database.store()
    second = alembic_database.store()
    receipt = await _persist(first, "lease", "d" * 64)
    now = datetime.now(UTC)
    start = asyncio.Event()

    async def contend(store: SQLiteStore, worker: str) -> JobLease | None:
        await start.wait()
        return await store.lease_next(worker, now)

    tasks = (
        asyncio.create_task(contend(first, "worker-a")),
        asyncio.create_task(contend(second, "worker-b")),
    )
    await asyncio.sleep(0)
    start.set()
    leases = await asyncio.gather(*tasks)
    assert sum(lease is not None for lease in leases) == 1
    async with first.connect() as connection:
        attempts = list(
            await connection.execute_fetchall(
                "SELECT worker_id FROM job_attempts WHERE job_id=?", (receipt.job_id,)
            )
        )
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_attempt_insert_failure_rolls_back_lease_transaction(
    alembic_database: MigratedDatabase,
) -> None:
    setup = alembic_database.store()
    receipt = await _persist(setup, "attempt-rollback", "e" * 64)

    def fail(stage: str) -> None:
        if stage == "after_attempt_insert":
            raise RuntimeError("test interruption")

    interrupted = alembic_database.store(failure_hook=fail)
    with pytest.raises(RuntimeError, match="test interruption"):
        await interrupted.lease_next("worker", datetime.now(UTC))
    job = await setup.get_job(cast(str, receipt.job_id))
    assert job is not None and job.state == "pending" and job.attempt_count == 0
    async with setup.connect() as connection:
        attempts = list(await connection.execute_fetchall("SELECT 1 FROM job_attempts"))
    assert attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_delivery_insert",
        "after_semantic_job_lookup",
        "after_capacity_count",
        "after_job_insert",
        "after_delivery_job_link",
    ],
)
async def test_ingress_failure_stages_roll_back_migrated_schema(
    alembic_database: MigratedDatabase,
    stage: str,
) -> None:
    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError("test interruption")

    store = alembic_database.store(failure_hook=fail)
    with pytest.raises(RuntimeError, match="test interruption"):
        await _persist(store, f"rollback-{stage}", "f" * 64)
    status = await alembic_database.store().status()
    assert status["deliveries"] == 0
    assert status["active_jobs"] == 0


@pytest.mark.asyncio
async def test_ingress_cancellation_rolls_back_migrated_schema(
    alembic_database: MigratedDatabase,
) -> None:
    def cancel(stage: str) -> None:
        if stage == "after_delivery_insert":
            raise asyncio.CancelledError

    store = alembic_database.store(failure_hook=cancel)
    with pytest.raises(asyncio.CancelledError):
        await _persist(store, "cancelled", "9" * 64)
    status = await alembic_database.store().status()
    assert status["deliveries"] == 0
    assert status["active_jobs"] == 0


@pytest.mark.asyncio
async def test_lifecycle_arrival_order_and_rollback_use_migrated_schema(
    alembic_database: MigratedDatabase,
) -> None:
    store = alembic_database.store()

    async def lifecycle(delivery: str, action: str, provider_time: datetime) -> None:
        normalization = normalize_webhook(
            "installation",
            delivery,
            {
                "action": action,
                "installation": {"id": 9, "updated_at": provider_time.isoformat()},
            },
        )
        await store.persist(
            provider_id="github",
            delivery_identity=f"github:{delivery}",
            event_name="installation",
            payload_sha256=(delivery + "0" * 64)[:64],
            normalization=normalization,
            received_at=datetime.now(UTC),
        )

    now = datetime.now(UTC)
    await lifecycle("suspended", "suspend", now + timedelta(seconds=1))
    await lifecycle("reactivated", "unsuspend", now)
    state = await store.installation_state("github", "9")
    assert state is not None and str(state.state) == "active"

    def fail(stage: str) -> None:
        if stage == "after_lifecycle_upsert":
            raise RuntimeError("test interruption")

    interrupted = alembic_database.store(failure_hook=fail)
    normalization = normalize_webhook(
        "installation",
        "rollback-lifecycle",
        {"action": "deleted", "installation": {"id": 9}},
    )
    with pytest.raises(RuntimeError, match="test interruption"):
        await interrupted.persist(
            provider_id="github",
            delivery_identity="github:rollback-lifecycle",
            event_name="installation",
            payload_sha256="8" * 64,
            normalization=normalization,
            received_at=datetime.now(UTC),
        )
    state = await store.installation_state("github", "9")
    assert state is not None and str(state.state) == "active"
    assert (await store.status())["deliveries"] == 2


@pytest.mark.asyncio
async def test_read_failures_are_sanitized_without_exception_chaining(
    alembic_database: MigratedDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = alembic_database.store()
    sentinel = "installation_states"
    async with store.connect() as connection:
        await connection.execute(f"DROP TABLE {sentinel}")
        await connection.commit()
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.installation_state("github", "9")
    error = caught.value
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sentinel not in caplog.text


def test_status_cli_reports_generic_sanitized_read_failure(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "queue_jobs_status_sentinel"
    connection = sqlite3.connect(alembic_database.path)
    try:
        connection.execute(f"ALTER TABLE queue_jobs RENAME TO {sentinel}")
        connection.commit()
    finally:
        connection.close()
    store = alembic_database.store()
    with pytest.raises(PersistenceUnavailableError) as repository_failure:
        asyncio.run(store.status())
    error = repository_failure.value
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    monkeypatch.setenv("REVIO_DATABASE_PATH", str(alembic_database.path))
    monkeypatch.setattr(sys, "argv", ["revio-queue-status"])
    with pytest.raises(SystemExit) as caught:
        queue_status.main()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "queue status is unavailable\n"
    assert sentinel not in captured.err
    assert str(alembic_database.path) not in captured.err
    assert sentinel not in captured.out
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_worker_run_contains_sanitized_read_failure(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = alembic_database.store()
    await _persist(store, "worker-read", "7" * 64)
    sentinel = "installation_states_worker_sentinel"
    async with store.connect() as connection:
        await connection.execute(f"ALTER TABLE installation_states RENAME TO {sentinel}")
        await connection.commit()
    original = store.installation_state
    normalized = asyncio.Event()
    captured_error: PersistenceUnavailableError | None = None

    async def installation_state(provider_id: str, installation_id: str):
        nonlocal captured_error
        try:
            return await original(provider_id, installation_id)
        except PersistenceUnavailableError as error:
            captured_error = error
            normalized.set()
            raise

    monkeypatch.setattr(store, "installation_state", installation_state)
    reader = _Reader()
    queue = QueueSettings()
    worker = QueueWorker(store, _processor(store, queue, reader), queue, "worker")
    running = asyncio.create_task(worker.run())
    await asyncio.wait_for(normalized.wait(), timeout=1)
    worker.request_stop()
    await asyncio.wait_for(running, timeout=1)
    assert captured_error is not None
    assert sentinel not in str(captured_error)
    assert sentinel not in repr(captured_error)
    assert captured_error.__cause__ is None
    assert captured_error.__context__ is None
    assert sentinel not in caplog.text
    assert reader.reads == 0


@pytest.mark.asyncio
async def test_shutdown_heartbeat_timeout_expiry_and_worker_recovery(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = QueueSettings.model_construct(
        queue_lease_seconds=10,
        queue_heartbeat_seconds=0.01,
        queue_shutdown_timeout_seconds=0.05,
    )
    store = alembic_database.store(queue)
    receipt = await _persist(store, "shutdown", "6" * 64)
    clock = _Clock(datetime.now(UTC))
    reader = _Reader(block=True)
    original_heartbeat = store.heartbeat
    heartbeat_during_grace = asyncio.Event()
    heartbeat_calls = 0

    async def heartbeat(lease: JobLease, now: datetime) -> bool:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        owned = await original_heartbeat(lease, now)
        if heartbeat_calls >= 2:
            heartbeat_during_grace.set()
        return owned

    monkeypatch.setattr(store, "heartbeat", heartbeat)
    worker = QueueWorker(
        store,
        _processor(store, queue, reader, clock=clock),
        queue,
        "old-worker",
        clock=clock,
    )
    running = asyncio.create_task(worker.run())
    await reader.started.wait()
    worker.request_stop()
    await asyncio.wait_for(heartbeat_during_grace.wait(), timeout=1)
    await asyncio.wait_for(running, timeout=1)
    calls_after_exit = heartbeat_calls
    await asyncio.sleep(0.02)
    assert heartbeat_calls == calls_after_exit
    old_job = await store.get_job(cast(str, receipt.job_id))
    assert old_job is not None and old_job.state == "running" and old_job.attempt_count == 1

    monkeypatch.setattr(store, "heartbeat", original_heartbeat)
    clock.current += timedelta(seconds=11)
    recovery_reader = _Reader()
    new_worker = QueueWorker(
        store,
        _processor(store, queue, recovery_reader, clock=clock),
        queue,
        "new-worker",
        clock=clock,
    )
    assert await new_worker.process_one()
    recovered = await store.get_job(cast(str, receipt.job_id))
    assert recovered is not None and recovered.state == "completed" and recovered.attempt_count == 2
    async with store.connect() as connection:
        attempts = list(
            await connection.execute_fetchall(
                "SELECT attempt_number, worker_id, outcome, finished_at "
                "FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
                (receipt.job_id,),
            )
        )
    assert [row["worker_id"] for row in attempts] == ["old-worker", "new-worker"]
    assert [row["outcome"] for row in attempts] == ["lease_expired", "completed"]
    assert all(row["finished_at"] is not None for row in attempts)


@pytest.mark.asyncio
async def test_retention_rolls_back_new_tombstone_before_later_conflict(
    alembic_database: MigratedDatabase,
) -> None:
    store = alembic_database.store()
    first = await _persist(store, "retention-first", "a" * 64, head="first")
    first_lease = await store.lease_next("worker", datetime.now(UTC))
    assert first_lease is not None
    second = await _persist(store, "retention-second", "b" * 64, head="second")
    second_lease = await store.lease_next("worker", datetime.now(UTC))
    assert second_lease is not None
    old = datetime.now(UTC) - timedelta(days=4)
    assert await store.complete(first_lease, head_sha="first", base_sha="base", now=old)
    assert await store.complete(
        second_lease,
        head_sha="second",
        base_sha="base",
        now=old + timedelta(seconds=1),
    )
    async with store.connect() as connection:
        await connection.execute(
            "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, ?)",
            (
                "github",
                "github:retention-second",
                "x" * 64,
                datetime.now(UTC).isoformat(),
            ),
        )
        await connection.commit()
    with pytest.raises(RetentionIntegrityError):
        await retain_terminal_history(store, age_days=1, batch_size=10, dry_run=False)
    async with store.connect() as connection:
        first_tombstone = list(
            await connection.execute_fetchall(
                "SELECT 1 FROM webhook_delivery_tombstones "
                "WHERE provider_id='github' AND delivery_identity='github:retention-first'"
            )
        )
        jobs = list(await connection.execute_fetchall("SELECT id FROM queue_jobs"))
        deliveries = list(await connection.execute_fetchall("SELECT id FROM webhook_deliveries"))
        attempts = list(await connection.execute_fetchall("SELECT id FROM job_attempts"))
    assert first_tombstone == []
    assert {row["id"] for row in jobs} == {first.job_id, second.job_id}
    assert len(deliveries) == len(attempts) == 2
