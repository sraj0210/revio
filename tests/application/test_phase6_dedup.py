"""Exact deduplication and durable publication projection."""

from datetime import UTC, datetime

import pytest

from tests.conftest import AlembicDatabase
from venaroq.adapters.persistence.sqlite.store import SQLiteStore
from venaroq.application.review.lifecycle import FindingLifecycleService
from venaroq.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)
from venaroq.domain.lifecycle import OccurrenceDisposition
from venaroq.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    Finding,
    FindingIdentity,
    TreeCollection,
)
from venaroq.domain.reviews import NormalizedReviewArtifact

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EVIDENCE = 'query = f"SELECT ... {name}"'


def _target(number: int = 7) -> ChangeRequestTarget:
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(installation=installation, external_id="1328857147")
    return ChangeRequestTarget(repository=repository, external_number=number)


def _cr(number: int = 7, head: str = "head") -> ChangeRequest:
    return ChangeRequest(target=_target(number), title="t", base_sha="base", head_sha=head)


def _diff() -> DiffCollection:
    return DiffCollection(
        items=(
            DiffFile(
                new_path="src/app/user.py",
                status="modified",
                lines=(DiffLine(content=EVIDENCE, side="new", new_line=10),),
            ),
        )
    )


def _finding(
    *, title: str = "SQL injection", evidence: str = EVIDENCE, inline: bool = True
) -> Finding:
    return Finding(
        category="security",
        title=title,
        explanation="The query interpolates untrusted input.",
        confidence=0.95,
        path="src/app/user.py",
        line=10,
        inline_eligible=inline,
        identity=FindingIdentity(rule="sql-injection", symbol="find_by_name", evidence=evidence),
    )


def _artifact(run_id: str, findings: tuple[Finding, ...]) -> NormalizedReviewArtifact:
    return NormalizedReviewArtifact(
        review_run_id=run_id,
        artifact_version="review-artifact-v2",
        schema_version="review-v2",
        prompt_version="review-v2",
        model_profile_id="review-default",
        model_profile_version="1",
        summary="Review complete.",
        findings=findings,
        partial=False,
        reason_codes=(),
        digest="0" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


class _Reader:
    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return _cr()

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return _diff()


class _Content:
    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        return None

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise NotImplementedError


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


def _service(store: SQLiteStore) -> FindingLifecycleService:
    return FindingLifecycleService(store.findings, _Reader(), _Content())


@pytest.mark.asyncio
async def test_new_finding_then_exact_duplicate(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    await _run(store, "run2", "job2")
    service = _service(store)
    projection = await service.deduplicate(
        run_id="run1",
        current=_cr(),
        artifact=_artifact("run1", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    assert projection.has_new
    assert len(projection.new) == 1
    occ1 = await store.findings.occurrences_for_run("run1")
    assert occ1[0].disposition == OccurrenceDisposition.NEW

    # Second run, same identity -> duplicate, not republished.
    projection2 = await service.deduplicate(
        run_id="run2",
        current=_cr(),
        artifact=_artifact("run2", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    assert not projection2.has_new
    occ2 = await store.findings.occurrences_for_run("run2")
    assert occ2[0].disposition == OccurrenceDisposition.EXACT_DUPLICATE


@pytest.mark.asyncio
async def test_changed_title_remains_duplicate(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    await _run(store, "run2", "job2")
    service = _service(store)
    await service.deduplicate(
        run_id="run1",
        current=_cr(),
        artifact=_artifact("run1", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    # Different generated wording, same identity (rule/symbol/evidence) -> still a duplicate.
    reworded = _finding(title="Totally different wording about the same bug")
    projection = await service.deduplicate(
        run_id="run2", current=_cr(), artifact=_artifact("run2", (reworded,)), diff=_diff(), now=NOW
    )
    assert not projection.has_new


@pytest.mark.asyncio
async def test_same_title_different_evidence_is_new(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    await _run(store, "run2", "job2")
    service = _service(store)
    await service.deduplicate(
        run_id="run1",
        current=_cr(),
        artifact=_artifact("run1", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    other_evidence = "value = other(name)"
    diff = DiffCollection(
        items=(
            DiffFile(
                new_path="src/app/user.py",
                status="modified",
                lines=(
                    DiffLine(content=EVIDENCE, side="new", new_line=10),
                    DiffLine(content=other_evidence, side="new", new_line=11),
                ),
            ),
        )
    )
    finding = _finding(evidence=other_evidence)
    projection = await service.deduplicate(
        run_id="run2", current=_cr(), artifact=_artifact("run2", (finding,)), diff=diff, now=NOW
    )
    assert projection.has_new


@pytest.mark.asyncio
async def test_invalid_evidence_is_omitted(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    service = _service(store)
    invented = _finding(evidence="this text is not present in the diff at all")
    projection = await service.deduplicate(
        run_id="run1", current=_cr(), artifact=_artifact("run1", (invented,)), diff=_diff(), now=NOW
    )
    assert not projection.has_new
    assert await store.findings.occurrences_for_run("run1") == ()


@pytest.mark.asyncio
async def test_intra_run_duplicate_collapses(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    service = _service(store)
    # Two findings with the same identity in one artifact collapse to a single publishable one.
    projection = await service.deduplicate(
        run_id="run1",
        current=_cr(),
        artifact=_artifact("run1", (_finding(), _finding(title="dup"))),
        diff=_diff(),
        now=NOW,
    )
    assert len(projection.new) == 1
    assert len(await store.findings.occurrences_for_run("run1")) == 1


@pytest.mark.asyncio
async def test_cross_pull_request_is_not_suppressed(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run-pr7", "job7")
    await _run(store, "run-pr8", "job8")
    service = _service(store)
    p7 = await service.deduplicate(
        run_id="run-pr7",
        current=_cr(7),
        artifact=_artifact("run-pr7", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    p8 = await service.deduplicate(
        run_id="run-pr8",
        current=_cr(8),
        artifact=_artifact("run-pr8", (_finding(),)),
        diff=_diff(),
        now=NOW,
    )
    # Same code in a different pull request is a distinct finding, not a suppressed duplicate.
    assert p7.has_new and p8.has_new


@pytest.mark.asyncio
async def test_summary_vs_inline_projection(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    await _run(store, "run1", "job1")
    service = _service(store)
    inline = _finding(evidence=EVIDENCE, inline=True)
    summary_only = _finding(title="other", evidence="value = other(name)", inline=False)
    diff = DiffCollection(
        items=(
            DiffFile(
                new_path="src/app/user.py",
                status="modified",
                lines=(
                    DiffLine(content=EVIDENCE, side="new", new_line=10),
                    DiffLine(content="value = other(name)", side="new", new_line=11),
                ),
            ),
        )
    )
    projection = await service.deduplicate(
        run_id="run1",
        current=_cr(),
        artifact=_artifact("run1", (inline, summary_only)),
        diff=diff,
        now=NOW,
    )
    assert len(projection.inline) == 1
    assert len(projection.summary_findings) == 1
