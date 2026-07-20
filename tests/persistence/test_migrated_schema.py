"""Repository behavior against the actual Alembic-created Phase 3 schema."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from alembic import command
from alembic.config import Config

from revio.adapters.persistence.sqlite import capabilities
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.config.database import DatabaseSettings


def _upgrade(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIO_DATABASE_PATH", str(path))
    command.upgrade(Config("alembic.ini"), "head")


def _objects(path: Path) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (kind, name, sql)
            for kind, name, sql in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%' "
                "AND name NOT IN ('alembic_version','sqlite_sequence') "
                "ORDER BY type, name"
            )
        ]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_repository_operates_on_alembic_schema_and_matches_test_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated_path = tmp_path / "migrated.db"
    _upgrade(migrated_path, monkeypatch)
    migrated = SQLiteStore(DatabaseSettings(database_path=migrated_path))
    normalization = normalize_webhook(
        "pull_request",
        "alembic",
        {
            "action": "opened",
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {
                "number": 3,
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            },
        },
    )
    receipt = await migrated.persist(
        provider_id="github",
        delivery_identity="github:alembic",
        event_name="pull_request",
        payload_sha256="a" * 64,
        normalization=normalization,
        received_at=datetime.now(UTC),
    )
    assert receipt.job_id is not None
    assert await migrated.lease_next("worker", datetime.now(UTC)) is not None
    assert await migrated.check_ready()

    initialized_path = tmp_path / "initialized.db"
    initialized = SQLiteStore(DatabaseSettings(database_path=initialized_path))
    await initialized.initialize()
    assert _objects(migrated_path) == _objects(initialized_path)


@pytest.mark.asyncio
async def test_complete_revision_set_and_verified_pragmas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "revio.db"
    _upgrade(path, monkeypatch)
    store = SQLiteStore(DatabaseSettings(database_path=path))
    async with store.connect() as connection:
        assert next(iter(await connection.execute_fetchall("PRAGMA journal_mode")))[0] == "wal"
        assert next(iter(await connection.execute_fetchall("PRAGMA synchronous")))[0] == 2
        assert next(iter(await connection.execute_fetchall("PRAGMA foreign_keys")))[0] == 1
        assert next(iter(await connection.execute_fetchall("PRAGMA busy_timeout")))[0] == 500
        await connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES ('unexpected_head')"
        )
        await connection.commit()
    assert not await store.check_ready()


@pytest.mark.asyncio
async def test_read_only_database_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "revio.db"
    _upgrade(path, monkeypatch)
    store = SQLiteStore(DatabaseSettings(database_path=path))
    path.chmod(0o400)
    try:
        assert not await store.check_ready()
    finally:
        path.chmod(0o600)


def test_minimum_sqlite_capability_and_destructive_downgrade_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capabilities.sqlite3, "sqlite_version_info", (3, 34, 9))
    with pytest.raises(RuntimeError, match=r"3\.35\.0"):
        SQLiteStore(DatabaseSettings(database_path=tmp_path / "old.db"))
    monkeypatch.undo()

    path = tmp_path / "revio.db"
    _upgrade(path, monkeypatch)
    with pytest.raises(RuntimeError, match="downgrade is unsupported"):
        command.downgrade(Config("alembic.ini"), "base")


@pytest.mark.asyncio
async def test_attempt_bounds_and_attempt_completion_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "revio.db"
    _upgrade(path, monkeypatch)
    store = SQLiteStore(DatabaseSettings(database_path=path))
    now = datetime.now(UTC).isoformat()
    async with store.connect() as connection:
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                "INSERT INTO queue_jobs "
                "(id,provider_id,job_type,semantic_identity,event_json,state,"
                "attempt_count,max_attempts,available_at,created_at,updated_at) "
                "VALUES ('bad','github','job','bad','{}','pending',2,1,?,?,?)",
                (now, now, now),
            )
        await connection.rollback()
        await connection.execute(
            "INSERT INTO queue_jobs "
            "(id,provider_id,job_type,semantic_identity,event_json,state,"
            "attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES ('job','github','job','job','{}','pending',0,1,?,?,?)",
            (now, now, now),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await connection.execute(
                "INSERT INTO job_attempts "
                "(job_id,attempt_number,worker_id,started_at,outcome) "
                "VALUES ('job',1,'worker',?,'completed')",
                (now,),
            )
        await connection.rollback()
