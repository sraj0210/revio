"""Ambiguity-safe thread resolution and latest-head race protection."""

from datetime import UTC, datetime

import pytest

from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.lifecycle import FindingLifecycleService
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)
from revio.domain.lifecycle import (
    FindingLifecycleState,
    FindingRecord,
    FindingRevalidationResult,
    RevalidationOutcome,
    ReviewThreadLink,
    ThreadLinkState,
    ThreadReadResult,
    ThreadResolutionState,
)
from revio.domain.models import ChangeRequest, DiffCollection, TreeCollection
from revio.errors import ProviderWriteAmbiguousError, ProviderWriteRejectedError
from tests.conftest import AlembicDatabase

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64
SECONDARY = "c" * 64


def _target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(installation=installation, external_id="1328857147")
    return ChangeRequestTarget(repository=repository, external_number=7)


def _cr(head: str = "head") -> ChangeRequest:
    return ChangeRequest(target=_target(), title="t", base_sha="base", head_sha=head)


def _record() -> FindingRecord:
    return FindingRecord(
        id="finding-1",
        provider_id="github",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=7,
        fingerprint_version="finding-v1",
        primary_fingerprint="b" * 64,
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


class _Reader:
    def __init__(self, head: str = "head") -> None:
        self.head = head

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return _cr(self.head)

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return DiffCollection()


class _Content:
    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        raise NotImplementedError

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise NotImplementedError


class _FakeResolver:
    def __init__(
        self,
        *,
        resolved: bool = False,
        found: bool = True,
        complete: bool = True,
        resolve_error: Exception | None = None,
        resolve_sets_resolved: bool = True,
    ) -> None:
        self.resolved = resolved
        self.found = found
        self.complete = complete
        self.resolve_error = resolve_error
        self.resolve_sets_resolved = resolve_sets_resolved
        self.mutations = 0
        self.reads = 0

    async def read_thread(
        self, target: ChangeRequestTarget, *, thread_node_id: str
    ) -> ThreadReadResult:
        self.reads += 1
        return ThreadReadResult(
            found=self.found,
            is_resolved=self.resolved,
            collection_complete=self.complete,
            thread_node_id=thread_node_id if self.found else None,
        )

    async def resolve_thread(self, target: ChangeRequestTarget, *, thread_node_id: str) -> None:
        self.mutations += 1
        if self.resolve_error is not None:
            if self.resolve_sets_resolved:
                self.resolved = True
            raise self.resolve_error
        self.resolved = True


async def _setup(
    store: SQLiteStore,
    *,
    outcome: RevalidationOutcome = RevalidationOutcome.FIXED,
    link_state: ThreadLinkState = ThreadLinkState.LINKED,
    fetched_ref: str = "head",
) -> None:
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
    await store.findings.upsert_finding(_record(), NOW)
    await store.findings.reserve_thread_link(
        ReviewThreadLink(
            finding_record_id="finding-1",
            review_run_id="run",
            marker_digest="m" * 24,
            marker_version="finding-marker-v1",
            state=ThreadLinkState.PENDING_DISCOVERY,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    if link_state == ThreadLinkState.LINKED:
        await store.findings.transition_thread_link(
            "finding-1",
            ThreadLinkState.PENDING_DISCOVERY,
            ThreadLinkState.LINKED,
            NOW,
            provider_thread_node_id="THREAD_1",
        )
    await store.findings.persist_revalidation_result(
        FindingRevalidationResult(
            review_run_id="run",
            finding_record_id="finding-1",
            outcome=outcome,
            reason_code=f"model_{outcome}",
            fetched_ref=fetched_ref,
            current_path="src/a.py",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _service(
    store: SQLiteStore, resolver: _FakeResolver, *, head: str = "head"
) -> FindingLifecycleService:
    return FindingLifecycleService(
        store.findings,
        reader=_Reader(head),
        content=_Content(),
        thread_resolver=resolver,
        marker_key=b"0" * 32,
    )


@pytest.mark.asyncio
async def test_fixed_linked_resolves_with_verification(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(resolved=False)
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 1
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.RESOLVED
    assert finding.resolved_head_sha == "head"
    op = await store.findings.get_resolution_operation("finding-1")
    assert op is not None and op.state == ThreadResolutionState.COMPLETED


@pytest.mark.asyncio
async def test_already_resolved_reconciles_without_mutation(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(resolved=True)
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 0
    op = await store.findings.get_resolution_operation("finding-1")
    assert op is not None and op.state == ThreadResolutionState.RECONCILED
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.RESOLVED


@pytest.mark.asyncio
async def test_ambiguous_without_proof_is_indeterminate_and_not_repeated(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(
        resolved=False,
        resolve_error=ProviderWriteAmbiguousError("lost"),
        resolve_sets_resolved=False,
    )
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 1
    op = await store.findings.get_resolution_operation("finding-1")
    assert op is not None and op.state == ThreadResolutionState.AMBIGUOUS
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None
    assert finding.state == FindingLifecycleState.RESOLUTION_INDETERMINATE
    # A later run must never repeat the mutation; it only re-reads.
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 1


@pytest.mark.asyncio
async def test_ambiguous_reconciled_when_reread_confirms(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store)
    # The mutation lands but the response is lost; a reread confirms resolution.
    resolver = _FakeResolver(
        resolved=False,
        resolve_error=ProviderWriteAmbiguousError("lost"),
        resolve_sets_resolved=True,
    )
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    op = await store.findings.get_resolution_operation("finding-1")
    assert op is not None and op.state == ThreadResolutionState.RECONCILED
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.RESOLVED


@pytest.mark.asyncio
async def test_known_rejection_leaves_finding_open(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(
        resolved=False,
        resolve_error=ProviderWriteRejectedError("forbidden"),
        resolve_sets_resolved=False,
    )
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    op = await store.findings.get_resolution_operation("finding-1")
    assert op is not None and op.state == ThreadResolutionState.KNOWN_REJECTED
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.OPEN


@pytest.mark.asyncio
async def test_newer_head_supersedes_resolution(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(resolved=False)
    service = _service(store, resolver, head="newer-head")
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 0
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.OPEN


@pytest.mark.asyncio
async def test_still_present_is_not_resolved(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _setup(store, outcome=RevalidationOutcome.STILL_PRESENT)
    resolver = _FakeResolver(resolved=False)
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 0
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.OPEN


@pytest.mark.asyncio
async def test_unlinked_fixed_is_not_resolved(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _setup(store, link_state=ThreadLinkState.PENDING_DISCOVERY)
    resolver = _FakeResolver(resolved=False)
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 0
    finding = await store.findings.get_finding("finding-1")
    assert finding is not None and finding.state == FindingLifecycleState.OPEN


@pytest.mark.asyncio
async def test_resolution_is_idempotent_on_restart(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _setup(store)
    resolver = _FakeResolver(resolved=False)
    service = _service(store, resolver)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    await service.resolve_threads(run_id="run", current=_cr(), validated_head="head", now=NOW)
    assert resolver.mutations == 1
