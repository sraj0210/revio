"""Durable Phase 6 finding-lifecycle persistence invariants."""

from datetime import UTC, datetime

import pytest

from tests.conftest import AlembicDatabase
from venaroq.adapters.persistence.sqlite.store import SQLiteStore
from venaroq.domain.lifecycle import (
    FindingLifecycleState,
    FindingOccurrence,
    FindingRecord,
    FindingRevalidationResult,
    FindingRevalidationRun,
    OccurrenceDisposition,
    RevalidationCallIdentity,
    RevalidationCallState,
    RevalidationOutcome,
    RevalidationRunState,
    ReviewThreadLink,
    ThreadLinkState,
    ThreadResolutionState,
)
from venaroq.domain.models import TokenUsage
from venaroq.domain.reviews import UsageDisposition
from venaroq.errors import PersistenceIntegrityError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64
PRIMARY = "b" * 64
SECONDARY = "c" * 64


async def _run(store: SQLiteStore, run_id: str = "run", job_id: str = "job") -> str:
    async with store.connect() as connection:
        stamp = NOW.isoformat()
        await connection.execute(
            "INSERT INTO queue_jobs (id,provider_id,job_type,semantic_identity,event_json,state,"
            "attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES (?,'github','review',?,'{}','pending',0,5,?,?,?)",
            (job_id, job_id, stamp, stamp, stamp),
        )
        await connection.commit()
    await store.reviews.create_run(
        run_id=run_id,
        job_id=job_id,
        head_sha="head",
        base_sha="base",
        external_id=f"external-{run_id}",
        now=NOW,
    )
    return run_id


def _record(run_id: str, *, primary: str = PRIMARY, number: int = 7) -> FindingRecord:
    return FindingRecord(
        id=f"finding-{primary[:8]}-{number}",
        provider_id="github",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=number,
        fingerprint_version="finding-v1",
        primary_fingerprint=primary,
        secondary_fingerprint=SECONDARY,
        path="src/app/user.py",
        category="security",
        rule="sql-injection",
        symbol="find_by_name",
        evidence="query = name",
        evidence_digest=DIGEST,
        state=FindingLifecycleState.OPEN,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        first_seen_head_sha="head",
        last_seen_head_sha="head",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_upsert_finding_new_then_duplicate(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    await _run(store, "run2", "job2")

    first = await store.findings.upsert_finding(_record("run1"), NOW)
    assert first.first_seen_run_id == "run1"
    assert first.state == FindingLifecycleState.OPEN

    # A later run with the same primary fingerprint is a duplicate: the record keeps run1 as
    # first_seen but advances last_seen.
    later = _record("run2").model_copy(
        update={"last_seen_run_id": "run2", "last_seen_head_sha": "h2"}
    )
    second = await store.findings.upsert_finding(later, NOW)
    assert second.id == first.id
    assert second.first_seen_run_id == "run1"
    assert second.last_seen_run_id == "run2"
    assert second.last_seen_head_sha == "h2"


@pytest.mark.asyncio
async def test_occurrence_idempotent_and_candidate_priors(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    await _run(store, "run2", "job2")
    record = await store.findings.upsert_finding(_record("run1"), NOW)
    occurrence = FindingOccurrence(
        finding_record_id=record.id,
        review_run_id="run1",
        artifact_ordinal=0,
        observed_head_sha="head",
        path="src/app/user.py",
        line=5,
        disposition=OccurrenceDisposition.NEW,
        publication_eligible=True,
        created_at=NOW,
    )
    await store.findings.record_occurrence(occurrence)
    await store.findings.record_occurrence(occurrence)  # idempotent
    occurrences = await store.findings.occurrences_for_run("run1")
    assert len(occurrences) == 1

    # Prior candidate query excludes findings first seen in the current run.
    assert (
        await store.findings.candidate_priors(
            provider_id="github",
            installation_id="174693302",
            repository_id="1328857147",
            external_number=7,
            current_run_id="run1",
        )
        == ()
    )
    priors = await store.findings.candidate_priors(
        provider_id="github",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=7,
        current_run_id="run2",
    )
    assert len(priors) == 1 and priors[0].id == record.id


@pytest.mark.asyncio
async def test_revalidation_run_results_and_calls(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record("run"), NOW)
    run = await store.findings.persist_revalidation_run(
        FindingRevalidationRun(
            review_run_id="run",
            validated_head_sha="head",
            selection_limit=25,
            selected_count=1,
            unselected_count=0,
            policy_digest=DIGEST,
            state=RevalidationRunState.SELECTED,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    assert run.state == RevalidationRunState.SELECTED
    assert await store.findings.transition_revalidation_run(
        "run", RevalidationRunState.SELECTED, RevalidationRunState.COMPLETED, NOW
    )

    identity = RevalidationCallIdentity(
        id="reval-call",
        review_run_id="run",
        batch_ordinal=1,
        provider_id="anthropic",
        model_profile_id="review-default",
        model_profile_version="1",
        prompt_version="revalidation-v1",
        schema_version="revalidation-v1",
    )
    call = await store.findings.reserve_revalidation_call(identity, NOW)
    assert call.state == RevalidationCallState.RESERVED
    assert await store.findings.transition_revalidation_call(
        "reval-call", RevalidationCallState.RESERVED, RevalidationCallState.ATTEMPT_STARTED, NOW
    )
    # An ambiguous call records unknown usage and is never retransmitted.
    assert await store.findings.transition_revalidation_call(
        "reval-call",
        RevalidationCallState.ATTEMPT_STARTED,
        RevalidationCallState.RESPONSE_OBSERVED,
        NOW,
        usage=UsageDisposition(status="known", usage=TokenUsage(uncached_input_tokens=5)),
    )
    usage = await store.findings.revalidation_usage("run")
    assert usage is not None and usage.status == "known"
    assert usage.usage is not None and usage.usage.uncached_input_tokens == 5

    await store.findings.persist_revalidation_result(
        FindingRevalidationResult(
            review_run_id="run",
            finding_record_id=record.id,
            outcome=RevalidationOutcome.FIXED,
            reason_code="model_fixed",
            fetched_ref="head",
            current_path="src/app/user.py",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    results = await store.findings.revalidation_results("run")
    assert len(results) == 1 and results[0].outcome == RevalidationOutcome.FIXED


@pytest.mark.asyncio
async def test_illegal_revalidation_call_transition_rejected(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store)
    identity = RevalidationCallIdentity(
        id="reval-call",
        review_run_id="run",
        batch_ordinal=1,
        provider_id="anthropic",
        model_profile_id="review-default",
        model_profile_version="1",
        prompt_version="revalidation-v1",
        schema_version="revalidation-v1",
    )
    await store.findings.reserve_revalidation_call(identity, NOW)
    with pytest.raises(PersistenceIntegrityError):
        await store.findings.transition_revalidation_call(
            "reval-call",
            RevalidationCallState.RESERVED,
            RevalidationCallState.COMPLETED,
            NOW,
        )


@pytest.mark.asyncio
async def test_thread_link_lifecycle(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record("run"), NOW)
    link = await store.findings.reserve_thread_link(
        ReviewThreadLink(
            finding_record_id=record.id,
            review_run_id="run",
            marker_digest="d" * 32,
            marker_version="marker-v1",
            state=ThreadLinkState.PENDING_DISCOVERY,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    assert link.state == ThreadLinkState.PENDING_DISCOVERY
    # Linking requires a thread node id.
    with pytest.raises(PersistenceIntegrityError):
        await store.findings.transition_thread_link(
            record.id, ThreadLinkState.PENDING_DISCOVERY, ThreadLinkState.LINKED, NOW
        )
    assert await store.findings.transition_thread_link(
        record.id,
        ThreadLinkState.PENDING_DISCOVERY,
        ThreadLinkState.LINKED,
        NOW,
        provider_thread_node_id="THREAD_1",
        provider_review_id="REVIEW_1",
    )
    linked = await store.findings.get_thread_link(record.id)
    assert linked is not None and linked.provider_thread_node_id == "THREAD_1"


@pytest.mark.asyncio
async def test_resolution_operation_and_mark_resolved(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record("run"), NOW)
    op = await store.findings.reserve_resolution_operation(
        operation_id="resop",
        finding_record_id=record.id,
        provider_thread_node_id="THREAD_1",
        head_sha="head",
        now=NOW,
    )
    assert op.state == ThreadResolutionState.RESERVED_UNATTEMPTED
    assert await store.findings.transition_resolution_operation(
        "resop",
        ThreadResolutionState.RESERVED_UNATTEMPTED,
        ThreadResolutionState.ATTEMPT_STARTED,
        NOW,
        increment_attempt=True,
    )
    assert await store.findings.transition_resolution_operation(
        "resop",
        ThreadResolutionState.ATTEMPT_STARTED,
        ThreadResolutionState.COMPLETED,
        NOW,
        provider_observed_state="resolved",
    )
    assert await store.findings.mark_finding_resolved(record.id, head_sha="head", now=NOW)
    resolved = await store.findings.get_finding(record.id)
    assert resolved is not None and resolved.state == FindingLifecycleState.RESOLVED
    assert resolved.resolved_head_sha == "head"
    # Already-resolved findings are not re-marked.
    assert not await store.findings.mark_finding_resolved(record.id, head_sha="head", now=NOW)


@pytest.mark.asyncio
async def test_mark_finding_indeterminate(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record("run"), NOW)
    assert await store.findings.mark_finding_indeterminate(record.id, NOW)
    updated = await store.findings.get_finding(record.id)
    assert updated is not None
    assert updated.state == FindingLifecycleState.RESOLUTION_INDETERMINATE
