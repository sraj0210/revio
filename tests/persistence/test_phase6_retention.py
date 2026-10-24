"""Phase 6 closed-finding retention boundaries and lifecycle readiness integrity."""

from datetime import UTC, datetime, timedelta

import pytest

from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.retention import retain_closed_findings
from revio.domain.lifecycle import (
    FindingLifecycleState,
    FindingOccurrence,
    FindingRecord,
    OccurrenceDisposition,
    ReviewThreadLink,
    ThreadLinkState,
    ThreadResolutionState,
)
from tests.conftest import AlembicDatabase

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=10)
DIGEST = "a" * 64
SECONDARY = "c" * 64


async def _run(store: SQLiteStore) -> None:
    async with store.connect() as connection:
        stamp = NOW.isoformat()
        await connection.execute(
            "INSERT INTO queue_jobs (id,provider_id,job_type,semantic_identity,event_json,state,"
            "attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES ('job','github','review','sem','{}','pending',0,5,?,?,?)",
            (stamp, stamp, stamp),
        )
        await connection.commit()
    await store.reviews.create_run(
        run_id="run", job_id="job", head_sha="head", base_sha="base", external_id="ext", now=NOW
    )


def _record(index: int) -> FindingRecord:
    return FindingRecord(
        id=f"finding-{index}",
        provider_id="github",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=7,
        fingerprint_version="finding-v1",
        primary_fingerprint=f"{index:064x}",
        secondary_fingerprint=SECONDARY,
        path="src/a.py",
        category="security",
        rule="sql-injection",
        symbol=None,
        evidence="query = name",
        evidence_digest=DIGEST,
        state=FindingLifecycleState.OPEN,
        first_seen_run_id="run",
        last_seen_run_id="run",
        first_seen_head_sha="head",
        last_seen_head_sha="head",
        created_at=NOW,
        updated_at=NOW,
    )


async def _resolved_finding(store: SQLiteStore, index: int, resolved_at: datetime) -> str:
    record = await store.findings.upsert_finding(_record(index), NOW)
    await store.findings.record_occurrence(
        FindingOccurrence(
            finding_record_id=record.id,
            review_run_id="run",
            artifact_ordinal=index,
            observed_head_sha="head",
            path="src/a.py",
            line=1,
            disposition=OccurrenceDisposition.NEW,
            publication_eligible=True,
            created_at=NOW,
        )
    )
    await store.findings.reserve_thread_link(
        ReviewThreadLink(
            finding_record_id=record.id,
            review_run_id="run",
            marker_digest=f"marker-{index:020d}",
            marker_version="finding-marker-v1",
            state=ThreadLinkState.PENDING_DISCOVERY,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await store.findings.transition_thread_link(
        record.id,
        ThreadLinkState.PENDING_DISCOVERY,
        ThreadLinkState.LINKED,
        NOW,
        provider_thread_node_id=f"THREAD_{index}",
    )
    await store.findings.reserve_resolution_operation(
        operation_id=f"resop-{index}",
        finding_record_id=record.id,
        provider_thread_node_id=f"THREAD_{index}",
        head_sha="head",
        now=NOW,
    )
    await store.findings.transition_resolution_operation(
        f"resop-{index}",
        ThreadResolutionState.RESERVED_UNATTEMPTED,
        ThreadResolutionState.RECONCILED,
        NOW,
        provider_observed_state="resolved",
    )
    await store.findings.mark_finding_resolved(record.id, head_sha="head", now=resolved_at)
    return record.id


@pytest.mark.asyncio
async def test_old_closed_finding_is_pruned_with_cascade(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store)
    old_id = await _resolved_finding(store, 1, OLD)
    result = await store.retain_closed_findings(
        cutoff=NOW - timedelta(days=180),
        batch_size=100,
        dry_run=False,
        correlation_id="test",
    )
    assert result.findings == 1
    assert result.occurrences == 1 and result.links == 1 and result.operations == 1
    assert await store.findings.get_finding(old_id) is None
    assert await store.findings.get_thread_link(old_id) is None
    assert await store.findings.get_resolution_operation(old_id) is None


@pytest.mark.asyncio
async def test_open_and_recent_findings_are_preserved(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store)
    open_record = await store.findings.upsert_finding(_record(1), NOW)
    recent_id = await _resolved_finding(store, 2, RECENT)
    result = await store.retain_closed_findings(
        cutoff=NOW - timedelta(days=180),
        batch_size=100,
        dry_run=False,
        correlation_id="test",
    )
    assert result.findings == 0
    assert await store.findings.get_finding(open_record.id) is not None
    assert await store.findings.get_finding(recent_id) is not None


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    old_id = await _resolved_finding(store, 1, OLD)
    result = await store.retain_closed_findings(
        cutoff=NOW - timedelta(days=180),
        batch_size=100,
        dry_run=True,
        correlation_id="test",
    )
    assert result.findings == 1
    assert await store.findings.get_finding(old_id) is not None


@pytest.mark.asyncio
async def test_coordinator_uses_age_days(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    # A finding resolved 200 days ago is eligible under the 180-day policy; a recent one is not.
    await _resolved_finding(store, 1, datetime.now(UTC) - timedelta(days=200))
    await _resolved_finding(store, 2, datetime.now(UTC) - timedelta(days=5))
    result = await retain_closed_findings(store, age_days=180, batch_size=100, dry_run=False)
    assert result.findings == 1


@pytest.mark.asyncio
async def test_integrity_failed_operation_fails_readiness(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record(1), NOW)
    assert await store.check_ready() is True
    await store.findings.reserve_resolution_operation(
        operation_id="resop",
        finding_record_id=record.id,
        provider_thread_node_id="THREAD_1",
        head_sha="head",
        now=NOW,
    )
    await store.findings.transition_resolution_operation(
        "resop",
        ThreadResolutionState.RESERVED_UNATTEMPTED,
        ThreadResolutionState.INTEGRITY_FAILED,
        NOW,
        terminal_reason="duplicate_linkage",
    )
    assert await store.check_ready() is False
