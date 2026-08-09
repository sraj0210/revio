"""Alembic-backed commit-disposition and ingress reconciliation regressions."""

import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr

from revio.adapters.observability import InMemoryQueueMetrics
from revio.adapters.persistence.sqlite.connection import CoordinationHook, FailureHook
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.api.app import create_app
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.domain.queue import IngressDisposition
from revio.errors import PersistenceIndeterminateError


class MigratedDatabase(Protocol):
    path: Path

    def store(
        self,
        queue: QueueSettings | None = None,
        *,
        failure_hook: FailureHook | None = None,
        coordination_hook: CoordinationHook | None = None,
    ) -> SQLiteStore: ...


def _settings(private_key: str) -> GitHubSettings:
    return GitHubSettings(
        environment="sandbox",
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(private_key),
        github_webhook_mode="durable",
        github_webhook_secret=SecretStr("commit-secret"),
    )


def _payload(event: str, *, action: str = "opened", head: str = "head") -> dict[str, Any]:
    if event == "installation":
        return {"action": action, "installation": {"id": 9}}
    return {
        "action": action,
        "installation": {"id": 9},
        "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
        "pull_request": {
            "number": 3,
            "base": {"sha": "base"},
            "head": {"sha": head},
        },
    }


def _signed_request(
    delivery: str,
    *,
    event: str = "pull_request",
    action: str = "opened",
    head: str = "head",
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(_payload(event, action=action, head=head)).encode()
    signature = hmac.new(b"commit-secret", body, hashlib.sha256).hexdigest()
    return body, {
        "content-type": "application/json",
        "x-github-delivery": delivery,
        "x-github-event": event,
        "x-hub-signature-256": f"sha256={signature}",
    }


async def _post(
    store: SQLiteStore,
    private_key: str,
    delivery: str,
    *,
    event: str = "pull_request",
    action: str = "opened",
    head: str = "head",
) -> Response:
    app = create_app(_settings(private_key), persistence=store)
    body, headers = _signed_request(delivery, event=event, action=action, head=head)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/webhooks/github", content=body, headers=headers)


def _commit_then_raise(
    monkeypatch: pytest.MonkeyPatch, sentinel: str
) -> tuple[Callable[[aiosqlite.Connection], Awaitable[None]], list[aiosqlite.Connection]]:
    original = aiosqlite.Connection.commit
    connections: list[aiosqlite.Connection] = []
    raised = False

    async def commit(connection: aiosqlite.Connection) -> None:
        nonlocal raised
        await original(connection)
        if not raised:
            raised = True
            connections.append(connection)
            raise aiosqlite.OperationalError(sentinel)

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit)
    return original, connections


async def _exact_replay(store: SQLiteStore, delivery: str) -> None:
    body, _ = _signed_request(delivery)
    receipt = await store.persist(
        provider_id="github",
        delivery_identity=f"github:{delivery}",
        event_name="pull_request",
        payload_sha256=hashlib.sha256(body).hexdigest(),
        normalization=normalize_webhook("pull_request", delivery, _payload("pull_request")),
        received_at=datetime.now(UTC),
    )
    assert receipt.disposition == IngressDisposition.IDEMPOTENT


@pytest.mark.asyncio
async def test_real_commit_then_close_failure_remains_accepted(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "post-commit-close-sensitive-sentinel"
    store = alembic_database.store()
    original_close = aiosqlite.Connection.close
    closed: list[aiosqlite.Connection] = []

    async def close_then_raise(connection: aiosqlite.Connection) -> None:
        await original_close(connection)
        if not closed:
            closed.append(connection)
            raise aiosqlite.OperationalError(sentinel)

    monkeypatch.setattr(aiosqlite.Connection, "close", close_then_raise)
    response = await _post(store, rsa_private_key_pem, "close-after-commit")
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    metrics = cast(InMemoryQueueMetrics, store.metrics)
    assert metrics.counters[("database_cleanup_error", None)] == 1
    assert closed
    with pytest.raises(ValueError, match="no active connection") as caught:
        _ = closed[0].in_transaction
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    monkeypatch.setattr(aiosqlite.Connection, "close", original_close)
    await _exact_replay(store, "close-after-commit")
    status = await store.status()
    assert status["deliveries"] == status["active_jobs"] == 1


@pytest.mark.asyncio
async def test_reconciliation_cannot_open_is_indeterminate_and_replay_is_safe(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "cannot-open-reconciliation-sentinel"
    original_commit, uncertain = _commit_then_raise(monkeypatch, sentinel)
    original_connect = aiosqlite.connect

    def connect(database: str | Path, **kwargs: Any) -> aiosqlite.Connection:
        if uncertain:
            raise aiosqlite.OperationalError(sentinel)
        return original_connect(database, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", connect)
    store = alembic_database.store()
    response = await _post(store, rsa_private_key_pem, "cannot-open")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    assert uncertain
    with pytest.raises(ValueError, match="no active connection"):
        _ = uncertain[0].in_transaction
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    monkeypatch.setattr(aiosqlite, "connect", original_connect)
    await _exact_replay(store, "cannot-open")
    assert (await store.status())["active_jobs"] == 1


@pytest.mark.asyncio
async def test_reconciliation_timeout_is_indeterminate_and_replay_is_safe(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    original_commit, _ = _commit_then_raise(monkeypatch, "timeout-sensitive-sentinel")
    never = asyncio.Event()

    async def coordinate(stage: str) -> None:
        if stage == "before_reconcile":
            await never.wait()

    store = alembic_database.store(coordination_hook=coordinate)
    response = await _post(store, rsa_private_key_pem, "timeout")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    await _exact_replay(alembic_database.store(), "timeout")


@pytest.mark.asyncio
async def test_reconciliation_cancellation_is_sanitized_indeterminate(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    original_commit, _ = _commit_then_raise(monkeypatch, "cancel-sensitive-sentinel")

    async def coordinate(stage: str) -> None:
        if stage == "before_reconcile":
            raise asyncio.CancelledError

    store = alembic_database.store(coordination_hook=coordinate)
    response = await _post(store, rsa_private_key_pem, "cancel-reconcile")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    await _exact_replay(alembic_database.store(), "cancel-reconcile")


@pytest.mark.asyncio
async def test_reconciliation_query_error_has_no_raw_exception_chain(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "query-reconciliation-sensitive-sentinel"
    original_commit, uncertain = _commit_then_raise(monkeypatch, sentinel)
    original_query = aiosqlite.Connection.execute_fetchall

    async def query(
        connection: aiosqlite.Connection, sql: str, parameters: object | None = None
    ) -> list[aiosqlite.Row]:
        if uncertain and "webhook_delivery_tombstones" in sql:
            raise aiosqlite.OperationalError(sentinel)
        if parameters is None:
            return list(await original_query(connection, sql))
        return list(await original_query(connection, sql, parameters))

    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", query)
    store = alembic_database.store()
    body, _ = _signed_request("query-error")
    with pytest.raises(PersistenceIndeterminateError) as caught:
        await store.persist(
            provider_id="github",
            delivery_identity="github:query-error",
            event_name="pull_request",
            payload_sha256=hashlib.sha256(body).hexdigest(),
            normalization=normalize_webhook(
                "pull_request", "query-error", _payload("pull_request")
            ),
            received_at=datetime.now(UTC),
        )
    error = caught.value
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", original_query)
    await _exact_replay(store, "query-error")


@pytest.mark.asyncio
async def test_reconciliation_missing_schema_is_indeterminate_and_ready_is_false(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "missing_schema_sensitive_sentinel"
    original_commit, _ = _commit_then_raise(monkeypatch, sentinel)

    async def break_schema(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path)
        try:
            connection.execute(f"ALTER TABLE webhook_deliveries RENAME TO {sentinel}")
            connection.commit()
        finally:
            connection.close()

    store = alembic_database.store(coordination_hook=break_schema)
    response = await _post(store, rsa_private_key_pem, "missing-schema")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    assert not await store.check_ready()
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    connection = sqlite3.connect(alembic_database.path)
    try:
        connection.execute(f"ALTER TABLE {sentinel} RENAME TO webhook_deliveries")
        connection.commit()
    finally:
        connection.close()
    await _exact_replay(alembic_database.store(), "missing-schema")


@pytest.mark.asyncio
async def test_reconciliation_real_exclusive_lock_is_indeterminate(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    original_commit, _ = _commit_then_raise(monkeypatch, "lock-sensitive-sentinel")
    locks: list[sqlite3.Connection] = []

    async def lock_database(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path, timeout=0)
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("UPDATE alembic_version SET version_num=version_num")
        locks.append(connection)

    store = alembic_database.store(coordination_hook=lock_database)
    response = await _post(store, rsa_private_key_pem, "locked-reconcile")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}
    for connection in locks:
        connection.rollback()
        connection.close()
    monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
    await _exact_replay(alembic_database.store(), "locked-reconcile")


@pytest.mark.asyncio
@pytest.mark.parametrize("contradiction", ["overlap", "partial_job"])
async def test_reconciliation_contradiction_is_integrity_error_and_fails_readiness(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    contradiction: str,
) -> None:
    delivery = f"integrity-{contradiction}"
    body, _ = _signed_request(delivery)
    payload_hash = hashlib.sha256(body).hexdigest()
    _commit_then_raise(monkeypatch, "integrity-sensitive-sentinel")

    async def contradict(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path)
        try:
            if contradiction == "overlap":
                connection.execute(
                    "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, datetime('now'))",
                    ("github", f"github:{delivery}", payload_hash),
                )
            else:
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.execute("DELETE FROM queue_jobs")
            connection.commit()
        finally:
            connection.close()

    store = alembic_database.store(coordination_hook=contradict)
    response = await _post(store, rsa_private_key_pem, delivery)
    assert response.status_code == 500
    assert response.json() == {"status": "integrity_error"}
    assert not await store.check_ready()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE webhook_deliveries SET provider_id='wrong-provider'",
        "UPDATE webhook_deliveries SET delivery_identity='github:wrong-identity'",
        "UPDATE webhook_deliveries SET disposition='ignored'",
        "UPDATE webhook_deliveries SET event_schema_version=2",
        "UPDATE webhook_deliveries SET normalized_event_json='{}'",
        "UPDATE webhook_deliveries SET semantic_identity='wrong-semantic'",
        "UPDATE webhook_deliveries SET linked_job_id=NULL",
        "UPDATE queue_jobs SET job_type='wrong-type'",
        "UPDATE queue_jobs SET semantic_identity='wrong-semantic'",
        "UPDATE queue_jobs SET event_schema_version=2",
        "UPDATE queue_jobs SET event_json='{}'",
    ],
)
async def test_reconciliation_verifies_complete_delivery_and_job_facts(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    mutation: str,
) -> None:
    _commit_then_raise(monkeypatch, "complete-facts-sensitive-sentinel")

    async def mutate(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(mutation)
            connection.commit()
        finally:
            connection.close()

    store = alembic_database.store(coordination_hook=mutate)
    response = await _post(store, rsa_private_key_pem, "complete-facts")
    assert response.status_code == 500
    assert response.json() == {"status": "integrity_error"}
    assert not await store.check_ready()


async def _post_lifecycle(
    store: SQLiteStore, private_key: str, delivery: str, action: str
) -> Response:
    return await _post(
        store,
        private_key,
        delivery,
        event="installation",
        action=action,
    )


@pytest.mark.asyncio
async def test_lifecycle_commit_reconciliation_confirms_current_source(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    _commit_then_raise(monkeypatch, "lifecycle-direct-sentinel")
    store = alembic_database.store()
    response = await _post_lifecycle(store, rsa_private_key_pem, "lifecycle-direct", "suspend")
    assert response.status_code == 202
    state = await store.installation_state("github", "9")
    assert state is not None and str(state.state) == "suspended"


@pytest.mark.asyncio
async def test_lifecycle_reconciliation_accepts_valid_later_source(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    original_commit, _ = _commit_then_raise(monkeypatch, "lifecycle-later-sentinel")
    later = alembic_database.store()
    ran = False

    async def persist_later(stage: str) -> None:
        nonlocal ran
        if stage != "before_reconcile" or ran:
            return
        ran = True
        monkeypatch.setattr(aiosqlite.Connection, "commit", original_commit)
        response = await _post_lifecycle(later, rsa_private_key_pem, "lifecycle-later", "unsuspend")
        assert response.status_code == 202

    first = alembic_database.store(coordination_hook=persist_later)
    response = await _post_lifecycle(first, rsa_private_key_pem, "lifecycle-first", "suspend")
    assert response.status_code == 202
    state = await first.installation_state("github", "9")
    assert state is not None and str(state.state) == "active"
    assert (await first.status())["deliveries"] == 2


@pytest.mark.asyncio
async def test_lifecycle_live_tombstone_overlap_is_integrity_error(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    delivery = "lifecycle-overlap"
    body, _ = _signed_request(delivery, event="installation", action="deleted")
    payload_hash = hashlib.sha256(body).hexdigest()
    _commit_then_raise(monkeypatch, "lifecycle-overlap-sentinel")

    async def overlap(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path)
        try:
            connection.execute(
                "INSERT INTO webhook_delivery_tombstones VALUES (?, ?, ?, datetime('now'))",
                ("github", f"github:{delivery}", payload_hash),
            )
            connection.commit()
        finally:
            connection.close()

    store = alembic_database.store(coordination_hook=overlap)
    response = await _post_lifecycle(store, rsa_private_key_pem, delivery, "deleted")
    assert response.status_code == 500
    assert response.json() == {"status": "integrity_error"}
    assert not await store.check_ready()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "contradiction",
    ["wrong_current_state", "missing_source", "non_lifecycle_source", "source_action_mismatch"],
)
async def test_lifecycle_reconciliation_rejects_impossible_source_state(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
    contradiction: str,
) -> None:
    setup = alembic_database.store()
    source_id: str | None = None
    if contradiction in {"non_lifecycle_source", "source_action_mismatch"}:
        source_delivery = (
            "non-lifecycle-source"
            if contradiction == "non_lifecycle_source"
            else "lifecycle-action-source"
        )
        response = (
            await _post(setup, rsa_private_key_pem, source_delivery)
            if contradiction == "non_lifecycle_source"
            else await _post_lifecycle(setup, rsa_private_key_pem, source_delivery, "unsuspend")
        )
        assert response.status_code == 202
        connection = sqlite3.connect(alembic_database.path)
        try:
            source_id = connection.execute(
                "SELECT id FROM webhook_deliveries WHERE delivery_identity=?",
                (f"github:{source_delivery}",),
            ).fetchone()[0]
        finally:
            connection.close()
    _commit_then_raise(monkeypatch, "lifecycle-integrity-sentinel")

    async def contradict(stage: str) -> None:
        if stage != "before_reconcile":
            return
        connection = sqlite3.connect(alembic_database.path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            if contradiction == "wrong_current_state":
                connection.execute("UPDATE installation_states SET state='active'")
            elif contradiction == "missing_source":
                connection.execute(
                    "UPDATE installation_states SET source_delivery_id='missing-source'"
                )
            elif contradiction in {"non_lifecycle_source", "source_action_mismatch"}:
                connection.execute(
                    "UPDATE installation_states SET source_delivery_id=?", (source_id,)
                )
            connection.commit()
        finally:
            connection.close()

    store = alembic_database.store(coordination_hook=contradict)
    response = await _post_lifecycle(store, rsa_private_key_pem, "lifecycle-impossible", "suspend")
    assert response.status_code == 500
    assert response.json() == {"status": "integrity_error"}
    assert not await store.check_ready()


@pytest.mark.asyncio
async def test_lifecycle_reconciliation_unavailable_is_indeterminate(
    alembic_database: MigratedDatabase,
    monkeypatch: pytest.MonkeyPatch,
    rsa_private_key_pem: str,
) -> None:
    _, uncertain = _commit_then_raise(monkeypatch, "lifecycle-unavailable-sentinel")
    original_query = aiosqlite.Connection.execute_fetchall
    failed = False

    async def query(
        connection: aiosqlite.Connection, sql: str, parameters: object | None = None
    ) -> list[aiosqlite.Row]:
        nonlocal failed
        if uncertain and "webhook_delivery_tombstones" in sql and not failed:
            failed = True
            raise aiosqlite.OperationalError("lifecycle-unavailable-sentinel")
        if parameters is None:
            return list(await original_query(connection, sql))
        return list(await original_query(connection, sql, parameters))

    monkeypatch.setattr(aiosqlite.Connection, "execute_fetchall", query)
    store = alembic_database.store()
    response = await _post_lifecycle(store, rsa_private_key_pem, "lifecycle-unavailable", "deleted")
    assert response.status_code == 500
    assert response.json() == {"status": "indeterminate"}


@pytest.mark.asyncio
async def test_ready_sanitizes_injected_read_failure(
    alembic_database: MigratedDatabase,
    rsa_private_key_pem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "readiness_sensitive_schema_sentinel"
    connection = sqlite3.connect(alembic_database.path)
    try:
        connection.execute(f"ALTER TABLE alembic_version RENAME TO {sentinel}")
        connection.commit()
    finally:
        connection.close()
    app = create_app(_settings(rsa_private_key_pem), persistence=alembic_database.store())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    assert sentinel not in response.text
    assert sentinel not in caplog.text
