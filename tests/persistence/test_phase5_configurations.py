"""Durable Phase 5 normalized configuration invariants."""

import asyncio
from datetime import UTC, datetime

import pytest

from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.domain.configuration import ConfigurationResolution, resolve_policy
from revio.errors import PersistenceIntegrityError
from tests.conftest import AlembicDatabase


async def _job(store: SQLiteStore, now: datetime) -> None:
    async with store.connect() as connection:
        stamp = now.isoformat()
        await connection.execute(
            "INSERT INTO queue_jobs (id,provider_id,job_type,semantic_identity,event_json,state,"
            "attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES ('job','github','review','semantic','{}','pending',0,5,?,?,?)",
            (stamp, stamp, stamp),
        )
        await connection.commit()


def _resolution(now: datetime) -> ConfigurationResolution:
    return ConfigurationResolution(
        job_id="job",
        provider_id="github",
        installation_id="123",
        repository_id="456",
        base_sha="base",
        status="valid",
        schema_version=1,
        source_digest="a" * 64,
        policy=resolve_policy(service_summary=0.6, service_inline=0.8),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_resolution_is_durable_idempotent_and_links_after_run_creation(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    resolution = _resolution(now)
    first, second = await asyncio.gather(
        store.configurations.persist(resolution),
        store.configurations.persist(resolution),
    )
    assert first == second == resolution
    await store.reviews.create_run(
        run_id="run",
        job_id="job",
        head_sha="head",
        base_sha="base",
        external_id="external",
        now=now,
    )
    assert await store.configurations.link_run("job", "run", now)
    assert (await store.configurations.get("job")).review_run_id == "run"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_conflicting_resolution_is_rejected(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    resolution = _resolution(now)
    await store.configurations.persist(resolution)
    with pytest.raises(PersistenceIntegrityError):
        await store.configurations.persist(resolution.model_copy(update={"base_sha": "other"}))


@pytest.mark.asyncio
async def test_raw_configuration_is_never_persisted(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    await store.configurations.persist(_resolution(now))
    assert b"version: 1" not in alembic_database.path.read_bytes()
