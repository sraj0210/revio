"""Durable Phase 4 ProviderCall and artifact invariants."""

import asyncio
from datetime import UTC, datetime

import pytest

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.execution import build_artifact
from revio.domain.reviews import (
    PartialReason,
    ProviderCallIdentity,
    ProviderCallState,
    ReviewRunState,
    UsageDisposition,
    WriteOperationState,
)
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


@pytest.mark.asyncio
async def test_provider_call_cas_usage_and_artifact_restart(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    run = await store.reviews.create_run(
        run_id="run",
        job_id="job",
        head_sha="head",
        base_sha="base",
        external_id="external",
        now=now,
    )
    identity = ProviderCallIdentity(
        id="call",
        review_run_id=run.id,
        call_kind="initial",
        call_ordinal=1,
        provider_id="anthropic",
        model_profile_id="review-default",
        model_profile_version="1",
        prompt_version="review-v1",
        schema_version="review-v1",
    )
    call = await store.reviews.reserve_call(identity, now)
    assert call.state == ProviderCallState.RESERVED
    assert await store.reviews.transition_call(
        call.identity.id,
        ProviderCallState.RESERVED,
        ProviderCallState.ATTEMPT_STARTED,
        now,
    )
    assert not await store.reviews.transition_call(
        call.identity.id,
        ProviderCallState.RESERVED,
        ProviderCallState.ATTEMPT_STARTED,
        now,
    )
    assert await store.reviews.transition_call(
        call.identity.id,
        ProviderCallState.ATTEMPT_STARTED,
        ProviderCallState.AMBIGUOUS,
        now,
        usage=UsageDisposition(status="unknown"),
    )
    artifact = build_artifact(
        run_id=run.id,
        profile=anthropic_model_profile(),
        summary="AI review outcome could not be confirmed.",
        findings=(),
        partial=True,
        reasons=(PartialReason.PROVIDER_CALL_AMBIGUOUS,),
        now=now,
    )
    assert await store.reviews.persist_artifact(artifact, ReviewRunState.GENERATION_PENDING)
    assert await store.reviews.get_artifact(run.id) == artifact


@pytest.mark.asyncio
async def test_write_attempt_state_and_marker_key_readiness(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    await store.reviews.create_run(
        run_id="run",
        job_id="job",
        head_sha="head",
        base_sha="base",
        external_id="external",
        now=now,
    )
    operation = await store.reviews.reserve_write_operation(
        "publish",
        operation_id="publish",
        run_id="run",
        head_sha="head",
        operation_key="operation-key",
        marker="<!-- revio:v1:review:abcdefghijklmnopqrstuvwx -->",
        marker_key_id="marker-key-id",
        now=now,
    )
    assert operation.state == WriteOperationState.RESERVED_UNATTEMPTED
    assert await store.reviews.transition_write_operation(
        "publish",
        operation.id,
        WriteOperationState.RESERVED_UNATTEMPTED,
        WriteOperationState.ATTEMPT_STARTED,
        now,
        increment_attempt=True,
    )
    assert await store.reviews.unresolved_marker_key_ids() == {"marker-key-id"}


@pytest.mark.asyncio
async def test_concurrent_provider_call_attempt_has_exactly_one_winner(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    await store.reviews.create_run(
        run_id="run",
        job_id="job",
        head_sha="head",
        base_sha="base",
        external_id="external",
        now=now,
    )
    identity = ProviderCallIdentity(
        id="call",
        review_run_id="run",
        call_kind="initial",
        call_ordinal=1,
        provider_id="anthropic",
        model_profile_id="review-default",
        model_profile_version="1",
        prompt_version="review-v1",
        schema_version="review-v1",
    )
    await asyncio.gather(*(store.reviews.reserve_call(identity, now) for _ in range(8)))
    winners = await asyncio.gather(
        *(
            store.reviews.transition_call(
                "call",
                ProviderCallState.RESERVED,
                ProviderCallState.ATTEMPT_STARTED,
                now,
            )
            for _ in range(8)
        )
    )
    assert winners.count(True) == 1


@pytest.mark.asyncio
async def test_illegal_provider_and_write_transitions_are_rejected(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _job(store, now)
    await store.reviews.create_run(
        run_id="run",
        job_id="job",
        head_sha="head",
        base_sha="base",
        external_id="external",
        now=now,
    )
    identity = ProviderCallIdentity(
        id="call",
        review_run_id="run",
        call_kind="initial",
        call_ordinal=1,
        provider_id="anthropic",
        model_profile_id="review-default",
        model_profile_version="1",
        prompt_version="review-v1",
        schema_version="review-v1",
    )
    await store.reviews.reserve_call(identity, now)
    with pytest.raises(PersistenceIntegrityError, match="illegal provider-call"):
        await store.reviews.transition_call(
            "call", ProviderCallState.RESERVED, ProviderCallState.COMPLETED, now
        )
    operation = await store.reviews.reserve_write_operation(
        "check_run",
        operation_id="operation",
        run_id="run",
        head_sha="head",
        external_id="check",
        now=now,
    )
    with pytest.raises(PersistenceIntegrityError, match="illegal write-operation"):
        await store.reviews.transition_write_operation(
            "check_run",
            operation.id,
            WriteOperationState.RESERVED_UNATTEMPTED,
            WriteOperationState.COMPLETED,
            now,
            provider_id="1",
        )
