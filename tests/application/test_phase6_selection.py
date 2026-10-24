"""Deterministic prior selection and exact-latest-head content collection."""

from datetime import UTC, datetime, timedelta

import pytest

from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.lifecycle import (
    AGGREGATE_BYTE_LIMIT,
    PER_FILE_BYTE_LIMIT,
    SELECTION_LIMIT,
    FindingLifecycleService,
    rank_candidates,
)
from revio.domain.configuration import EffectiveReviewPolicy
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)
from revio.domain.lifecycle import (
    FindingLifecycleState,
    FindingRecord,
    OccurrenceDisposition,
    RevalidationOutcome,
)
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    TreeCollection,
)
from revio.errors import ProviderTransientError
from tests.conftest import AlembicDatabase

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64
SECONDARY = "c" * 64


def _target() -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(installation=installation, external_id="1328857147")
    return ChangeRequestTarget(repository=repository, external_number=7)


def _change_request(head: str = "head") -> ChangeRequest:
    return ChangeRequest(target=_target(), title="t", base_sha="base", head_sha=head)


def _policy() -> EffectiveReviewPolicy:
    return EffectiveReviewPolicy(summary_confidence=0.6, inline_confidence=0.8)


def _record(index: int, *, path: str = "src/a.py", seen: datetime = NOW) -> FindingRecord:
    primary = f"{index:064x}"
    return FindingRecord(
        id=f"finding-{index}",
        provider_id="github",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=7,
        fingerprint_version="finding-v1",
        primary_fingerprint=primary,
        secondary_fingerprint=SECONDARY,
        path=path,
        category="security",
        rule="sql-injection",
        symbol=None,
        evidence="query = name",
        evidence_digest=DIGEST,
        state=FindingLifecycleState.OPEN,
        first_seen_run_id="prior-run",
        last_seen_run_id="prior-run",
        first_seen_head_sha="old",
        last_seen_head_sha="old",
        created_at=seen,
        updated_at=seen,
    )


class _FakeReader:
    def __init__(self, change_request: ChangeRequest) -> None:
        self._change_request = change_request

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return self._change_request

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return DiffCollection()


class _FakeContent:
    def __init__(self, files: dict[str, object]) -> None:
        self._files = files

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        value = self._files.get(path, KeyError)
        if value is KeyError:
            return None
        if isinstance(value, Exception):
            raise value
        return str(value)

    async def get_tree(
        self, target: ChangeRequestTarget, path: str, ref: str
    ) -> TreeCollection:
        raise NotImplementedError


def test_rank_orders_changed_then_observed_then_recency() -> None:
    old = _record(1, path="src/x.py", seen=NOW)
    changed = _record(2, path="src/a.py", seen=NOW - timedelta(days=5))
    observed = _record(3, path="src/y.py", seen=NOW)
    ranked = rank_candidates(
        (old, changed, observed),
        changed_paths=frozenset({"src/a.py"}),
        observed_again=frozenset({"finding-3"}),
    )
    # changed-path wins first, then observed-again, then the remaining by recency.
    assert [r.id for r in ranked] == ["finding-2", "finding-3", "finding-1"]


async def _run(store: SQLiteStore, run_id: str, job_id: str) -> None:
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
        external_id=f"ext-{run_id}",
        now=NOW,
    )


@pytest.mark.asyncio
async def test_selection_limit_and_durable_decisions(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    # 30 prior findings, all on a changed path so ranking is stable by id.
    for index in range(30):
        await store.findings.upsert_finding(_record(index), NOW)
    service = FindingLifecycleService(
        store.findings, _FakeReader(_change_request()), _FakeContent({})
    )
    diff = DiffCollection(
        items=(
            DiffFile(
                new_path="src/a.py",
                status="modified",
                lines=(DiffLine(content="x", side="new", new_line=1),),
            ),
        )
    )
    outcome = await service.select_priors(
        run_id="run",
        current=_change_request(),
        diff=diff,
        policy=_policy(),
        now=NOW,
    )
    assert len(outcome.selected) == SELECTION_LIMIT
    assert len(outcome.unselected) == 30 - SELECTION_LIMIT
    run = await store.findings.get_revalidation_run("run")
    assert run is not None
    assert run.selected_count == SELECTION_LIMIT
    assert run.unselected_count == 5
    # Unselected priors get durable uncertain results immediately.
    results = await store.findings.revalidation_results("run")
    assert len(results) == 5
    assert all(r.outcome == RevalidationOutcome.UNCERTAIN for r in results)
    # Occurrences record selected/unselected dispositions.
    occurrences = await store.findings.occurrences_for_run("run")
    selected_prior = [
        o for o in occurrences if o.disposition == OccurrenceDisposition.SELECTED_PRIOR
    ]
    assert len(selected_prior) == SELECTION_LIMIT


@pytest.mark.asyncio
async def test_selection_is_idempotent_on_restart(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    await store.findings.upsert_finding(_record(1), NOW)
    service = FindingLifecycleService(
        store.findings, _FakeReader(_change_request()), _FakeContent({})
    )
    for _ in range(2):
        outcome = await service.select_priors(
            run_id="run",
            current=_change_request(),
            diff=DiffCollection(),
            policy=_policy(),
            now=NOW,
        )
        assert len(outcome.selected) == 1
    occurrences = await store.findings.occurrences_for_run("run")
    assert len(occurrences) == 1


@pytest.mark.asyncio
async def test_excluded_path_is_unselected_policy_excluded(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    await store.findings.upsert_finding(_record(1, path="vendor/lib.py"), NOW)
    policy = EffectiveReviewPolicy(
        summary_confidence=0.6, inline_confidence=0.8, exclusions=(".revio.yml", "vendor/**")
    )
    service = FindingLifecycleService(
        store.findings, _FakeReader(_change_request()), _FakeContent({})
    )
    outcome = await service.select_priors(
        run_id="run",
        current=_change_request(),
        diff=DiffCollection(),
        policy=policy,
        now=NOW,
    )
    assert outcome.selected == ()
    assert outcome.unselected[0][1] == "policy_excluded"


@pytest.mark.asyncio
async def test_collect_content_uses_exact_head_and_bounds(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    present = await store.findings.upsert_finding(_record(1, path="src/present.py"), NOW)
    missing = await store.findings.upsert_finding(_record(2, path="src/missing.py"), NOW)
    oversized = await store.findings.upsert_finding(_record(3, path="src/big.py"), NOW)
    content = _FakeContent(
        {
            "src/present.py": "def f(): return 1",
            "src/big.py": "x" * (PER_FILE_BYTE_LIMIT + 1),
        }
    )
    service = FindingLifecycleService(store.findings, _FakeReader(_change_request()), content)
    outcome = await service.collect_content(
        run_id="run",
        target=_target(),
        validated_head="head",
        selected=(present, missing, oversized),
        policy=_policy(),
        now=NOW,
    )
    assert [record.id for record, _ in outcome.items] == ["finding-1"]
    results = {r.finding_record_id: r for r in await store.findings.revalidation_results("run")}
    assert results["finding-2"].reason_code == "content_missing"
    assert results["finding-3"].reason_code == "content_oversized"
    assert results["finding-2"].fetched_ref == "head"


@pytest.mark.asyncio
async def test_collect_content_supersedes_on_head_change(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    record = await store.findings.upsert_finding(_record(1), NOW)
    reader = _FakeReader(_change_request(head="newer-head"))
    service = FindingLifecycleService(store.findings, reader, _FakeContent({"src/a.py": "x"}))
    outcome = await service.collect_content(
        run_id="run",
        target=_target(),
        validated_head="head",
        selected=(record,),
        policy=_policy(),
        now=NOW,
    )
    assert outcome.superseded is True
    assert outcome.items == []


@pytest.mark.asyncio
async def test_collect_content_transient_propagates(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    record = await store.findings.upsert_finding(_record(1), NOW)
    content = _FakeContent({"src/a.py": ProviderTransientError("rate limited")})
    service = FindingLifecycleService(store.findings, _FakeReader(_change_request()), content)
    with pytest.raises(ProviderTransientError):
        await service.collect_content(
            run_id="run",
            target=_target(),
            validated_head="head",
            selected=(record,),
            policy=_policy(),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_collect_content_aggregate_limit(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "prior-run", "prior-job")
    await _run(store, "run", "job")
    big = "x" * (PER_FILE_BYTE_LIMIT - 1)
    records = [
        await store.findings.upsert_finding(_record(i, path=f"src/f{i}.py"), NOW) for i in range(6)
    ]
    content = _FakeContent({f"src/f{i}.py": big for i in range(6)})
    service = FindingLifecycleService(store.findings, _FakeReader(_change_request()), content)
    outcome = await service.collect_content(
        run_id="run",
        target=_target(),
        validated_head="head",
        selected=tuple(records),
        policy=_policy(),
        now=NOW,
    )
    # 4 files of ~128 KiB fit under the 512 KiB aggregate ceiling; the rest become uncertain.
    assert len(outcome.items) <= AGGREGATE_BYTE_LIMIT // (PER_FILE_BYTE_LIMIT - 1)
    reasons = {r.reason_code for r in await store.findings.revalidation_results("run")}
    assert "aggregate_limit" in reasons
