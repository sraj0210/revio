"""Per-finding marker linkage and durable thread discovery."""

from datetime import UTC, datetime

import pytest

from tests.conftest import AlembicDatabase
from venaroq.adapters.persistence.sqlite.store import SQLiteStore
from venaroq.application.review.lifecycle import FindingLifecycleService
from venaroq.application.review.markers import finding_marker, finding_marker_token, review_marker
from venaroq.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)
from venaroq.domain.lifecycle import (
    FindingLifecycleState,
    FindingRecord,
    ThreadDiscoveryResult,
    ThreadLinkState,
    ThreadReadResult,
)
from venaroq.domain.models import ChangeRequest, DiffCollection, TreeCollection

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KEY = b"0" * 32
DIGEST = "a" * 64
SECONDARY = "c" * 64


def _target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(installation=installation, external_id="1328857147")
    return ChangeRequestTarget(repository=repository, external_number=7)


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


class _FakeThreadReader:
    def __init__(self, result: ThreadDiscoveryResult) -> None:
        self.result = result
        self.markers: list[str] = []

    async def discover_thread(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ThreadDiscoveryResult:
        self.markers.append(marker)
        return self.result


class _FakeResolver:
    async def read_thread(
        self, target: ChangeRequestTarget, *, thread_node_id: str
    ) -> ThreadReadResult:
        raise NotImplementedError

    async def resolve_thread(self, target: ChangeRequestTarget, *, thread_node_id: str) -> None:
        raise NotImplementedError


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


def _service(store: SQLiteStore, reader: _FakeThreadReader) -> FindingLifecycleService:
    return FindingLifecycleService(
        store.findings,
        reader=_RaisingReader(),
        content=_RaisingContent(),
        thread_reader=reader,
        thread_resolver=_FakeResolver(),
        marker_key=KEY,
    )


class _RaisingReader:
    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        raise NotImplementedError

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        raise NotImplementedError


class _RaisingContent:
    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        raise NotImplementedError

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise NotImplementedError


def test_finding_marker_is_deterministic_and_distinct() -> None:
    token = finding_marker_token(KEY, "finding-1")
    assert finding_marker_token(KEY, "finding-1") == token
    assert finding_marker_token(KEY, "finding-2") != token
    marker = finding_marker(token)
    assert marker.startswith("<!-- venaroq:v1:finding:")
    # Per-finding markers never collide with the review-level marker namespace.
    assert "venaroq:v1:review:" not in marker
    assert review_marker(KEY, "op") != marker


@pytest.mark.asyncio
async def test_single_match_links_thread(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record(1), NOW)
    service = _service(
        store,
        _FakeThreadReader(
            ThreadDiscoveryResult(
                match_count=1,
                collection_complete=True,
                pages_inspected=1,
                items_inspected=1,
                thread_node_ids=("T1",),
                resolved_states=(False,),
                review_ids=("R1",),
                comment_ids=("C1",),
            )
        ),
    )
    await service.register_pending_link(
        run_id="run", finding_record_id=record.id, provider_review_id="R1", now=NOW
    )
    await service.discover_and_link(run_id="run", target=_target(), now=NOW)
    link = await store.findings.get_thread_link(record.id)
    assert link is not None
    assert link.state == ThreadLinkState.LINKED
    assert link.provider_thread_node_id == "T1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ThreadDiscoveryResult(
            match_count=0, collection_complete=True, pages_inspected=1, items_inspected=1
        ),
        ThreadDiscoveryResult(
            match_count=2,
            collection_complete=True,
            pages_inspected=1,
            items_inspected=2,
            thread_node_ids=("T1", "T2"),
            resolved_states=(False, False),
        ),
        ThreadDiscoveryResult(
            match_count=1,
            collection_complete=False,
            pages_inspected=5,
            items_inspected=5,
            service_limit_reached=True,
            thread_node_ids=("T1",),
            resolved_states=(False,),
        ),
    ],
)
async def test_non_unique_or_incomplete_is_indeterminate(
    alembic_database: AlembicDatabase, result: ThreadDiscoveryResult
) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record(1), NOW)
    service = _service(store, _FakeThreadReader(result))
    await service.register_pending_link(
        run_id="run", finding_record_id=record.id, provider_review_id=None, now=NOW
    )
    await service.discover_and_link(run_id="run", target=_target(), now=NOW)
    link = await store.findings.get_thread_link(record.id)
    assert link is not None
    assert link.state == ThreadLinkState.DISCOVERY_INDETERMINATE


@pytest.mark.asyncio
async def test_summary_finding_is_not_threaded(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store)
    record = await store.findings.upsert_finding(_record(1), NOW)
    reader = _FakeThreadReader(
        ThreadDiscoveryResult(
            match_count=0, collection_complete=True, pages_inspected=0, items_inspected=0
        )
    )
    service = _service(store, reader)
    await service.register_not_threaded(run_id="run", finding_record_id=record.id, now=NOW)
    await service.discover_and_link(run_id="run", target=_target(), now=NOW)
    link = await store.findings.get_thread_link(record.id)
    assert link is not None
    assert link.state == ThreadLinkState.NOT_THREADED
    # A not_threaded finding is never queried for discovery.
    assert reader.markers == []
