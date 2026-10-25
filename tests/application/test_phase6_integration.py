"""End-to-end Phase 6 lifecycle through ReviewJobExecutor.execute."""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from tests.conftest import AlembicDatabase
from venaroq.adapters.ai.anthropic import anthropic_model_profile
from venaroq.adapters.persistence.sqlite.store import SQLiteStore
from venaroq.adapters.scm.github.webhook.normalizer import normalize_webhook
from venaroq.application.review.execution import ReviewGenerationService
from venaroq.application.review.job import ReviewJobExecutor, stable_id
from venaroq.application.review.lifecycle import FindingLifecycleService
from venaroq.config.review import ReviewSettings
from venaroq.domain.identifiers import ChangeRequestTarget
from venaroq.domain.lifecycle import (
    FindingLifecycleState,
    OccurrenceDisposition,
    RevalidationOutcome,
    ThreadDiscoveryResult,
    ThreadLinkState,
    ThreadReadResult,
)
from venaroq.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    Finding,
    FindingIdentity,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
    TreeCollection,
)
from venaroq.domain.revalidation import (
    FindingRevalidationBatchResult,
    FindingRevalidationRequest,
    RevalidationResultItem,
)
from venaroq.domain.reviews import ReconciliationResult, ReviewStatusDetails

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EVIDENCE = "ok = dangerous(name)"
MARKER_KEY = b"test-marker-key-with-sufficient-entropy"


def _diff() -> DiffCollection:
    return DiffCollection(
        items=(
            DiffFile(
                new_path="a.py",
                status="modified",
                lines=(DiffLine(content=EVIDENCE, side="new", new_line=1),),
            ),
        ),
        expected_file_count=1,
    )


def _finding() -> Finding:
    return Finding(
        category="security",
        title="Dangerous call",
        explanation="Untrusted input flows into a dangerous call.",
        confidence=0.95,
        path="a.py",
        line=1,
        identity=FindingIdentity(rule="dangerous-call", symbol="handler", evidence=EVIDENCE),
    )


class _Reviewer:
    def __init__(self, results: list[ReviewResult]) -> None:
        self.results = results
        self.calls = 0

    async def preflight(self, request: ReviewRequest) -> dict[str, object]:
        return {"request": request}

    async def repair_preflight(self, payload: dict[str, object]) -> dict[str, object]:
        return payload

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class _Reader:
    def __init__(self, target: ChangeRequestTarget) -> None:
        self.target = target
        self.head = "head"

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return ChangeRequest(target=target, title="Change", base_sha="base", head_sha=self.head)

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return _diff()

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        # A distinctive token proves whole-file content is never persisted anywhere.
        return f"def handler():  # VENAROQ_PRIVACY_FILE_BODY_TOKEN\n    {EVIDENCE}\n"

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise NotImplementedError


class _Revalidator:
    def __init__(self, outcome: RevalidationOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def preflight_revalidation(
        self, request: FindingRevalidationRequest
    ) -> dict[str, object]:
        return {
            "_venaroq_estimated_input_tokens": 10,
            "_ids": [item.finding_id for item in request.items],
        }

    async def generate_revalidation(
        self, payload: dict[str, object]
    ) -> FindingRevalidationBatchResult:
        self.calls += 1
        ids = [i for i in payload["_ids"] if isinstance(i, str)]  # type: ignore[union-attr]
        return FindingRevalidationBatchResult(
            results=tuple(
                RevalidationResultItem(finding_id=fid, outcome=self.outcome) for fid in ids
            ),
            usage=TokenUsage(output_tokens=1),
        )


class _ThreadReader:
    def __init__(self) -> None:
        self.calls = 0

    async def discover_thread(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ThreadDiscoveryResult:
        self.calls += 1
        return ThreadDiscoveryResult(
            match_count=1,
            collection_complete=True,
            pages_inspected=1,
            items_inspected=1,
            thread_node_ids=("THREAD_1",),
            resolved_states=(False,),
            review_ids=("review-1",),
            comment_ids=("comment-1",),
        )


class _ThreadResolver:
    def __init__(self) -> None:
        self.mutations = 0
        self.resolved = False

    async def read_thread(
        self, target: ChangeRequestTarget, *, thread_node_id: str
    ) -> ThreadReadResult:
        return ThreadReadResult(
            found=True,
            is_resolved=self.resolved,
            collection_complete=True,
            thread_node_id=thread_node_id,
        )

    async def resolve_thread(self, target: ChangeRequestTarget, *, thread_node_id: str) -> None:
        self.mutations += 1
        self.resolved = True


class _Writer:
    def __init__(self) -> None:
        self.check_id: str | None = None
        self.review_id: str | None = None
        self.publish_calls = 0
        self.published_markers: list[tuple[str, ...]] = []
        self.remote_check: dict[str, object] = {}
        self.final_summaries: list[str] = []

    async def reconcile_check_run(
        self,
        target: ChangeRequestTarget,
        *,
        head_sha: str,
        external_id: str,
        name: str = "Venaroq review",
    ) -> ReconciliationResult:
        ids = (self.check_id,) if self.check_id else ()
        return ReconciliationResult(
            match_count=len(ids),
            collection_complete=True,
            pages_inspected=1,
            items_inspected=len(ids),
            provider_ids=ids,
        )

    async def create_check_run(
        self, target: ChangeRequestTarget, details: ReviewStatusDetails
    ) -> str:
        self.check_id = "check-1"
        self.remote_check = {
            "status": "queued",
            "conclusion": None,
            "output": {"summary": details.summary},
        }
        return self.check_id

    async def update_check_run(
        self, target: ChangeRequestTarget, provider_id: str, details: ReviewStatusDetails
    ) -> None:
        if details.status == "completed":
            self.final_summaries.append(details.summary)
        self.remote_check = {
            "status": details.status,
            "conclusion": details.conclusion,
            "output": {"summary": details.summary},
        }

    async def get_check_run(
        self, target: ChangeRequestTarget, provider_id: str
    ) -> dict[str, object]:
        return dict(self.remote_check)

    async def reconcile_review(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ReconciliationResult:
        ids = (self.review_id,) if self.review_id is not None else ()
        return ReconciliationResult(
            match_count=len(ids),
            collection_complete=True,
            pages_inspected=1,
            items_inspected=len(ids),
            provider_ids=ids,
        )

    def validate_inline_findings(
        self, findings: tuple[Finding, ...], diff: DiffCollection
    ) -> tuple[Finding, ...]:
        return findings

    async def publish_review(
        self,
        target: ChangeRequestTarget,
        *,
        summary: str,
        findings: tuple[Finding, ...],
        marker: str,
        commit_id: str,
        markers: tuple[str, ...] = (),
    ) -> str:
        self.publish_calls += 1
        self.published_markers.append(markers)
        self.review_id = "review-1"
        return self.review_id


def _settings() -> ReviewSettings:
    return ReviewSettings(
        environment="sandbox",
        review_enabled=True,
        review_publish_enabled=True,
        publish_marker_key=SecretStr("test-marker-key-with-sufficient-entropy"),
    )


async def _lease(store: SQLiteStore, *, head: str, delivery: str):
    normalization = normalize_webhook(
        "pull_request",
        delivery,
        {
            "action": "synchronize",
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {"number": 3, "base": {"sha": "base"}, "head": {"sha": head}},
        },
    )
    await store.persist(
        provider_id="github",
        delivery_identity=f"github:{delivery}",
        event_name="pull_request",
        payload_sha256="a" * 64,
        normalization=normalization,
        received_at=NOW,
    )
    lease = await store.lease_next("worker", NOW)
    assert lease is not None and lease.job.event.change_request is not None
    current = ChangeRequest(
        target=lease.job.event.change_request, title="Change", base_sha="base", head_sha=head
    )
    return lease, current


def _executor(
    store: SQLiteStore,
    reader: _Reader,
    reviewer: _Reviewer,
    writer: _Writer,
    lifecycle: FindingLifecycleService,
) -> ReviewJobExecutor:
    settings = _settings()
    return ReviewJobExecutor(
        store.reviews,
        reader,
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=MARKER_KEY,
        lifecycle=lifecycle,
    )


@pytest.mark.asyncio
async def test_new_finding_published_with_marker_and_linked(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store, head="head", delivery="d1")
    reader = _Reader(current.target)
    writer = _Writer()
    lifecycle = FindingLifecycleService(
        store.findings,
        reader,
        reader,
        revalidator=_Revalidator(RevalidationOutcome.STILL_PRESENT),
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    executor = _executor(
        store,
        reader,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        writer,
        lifecycle,
    )
    outcome = await executor.execute(lease, current, now=NOW)
    assert outcome == "completed"
    assert writer.publish_calls == 1
    # The inline comment carried a hidden per-finding marker.
    assert writer.published_markers[0] and writer.published_markers[0][0].startswith(
        "<!-- venaroq:v1:finding:"
    )
    run_id = stable_id("review-run", lease.job.id)
    occurrences = await store.findings.occurrences_for_run(run_id)
    assert any(o.disposition == OccurrenceDisposition.NEW for o in occurrences)
    # Discovery linked the thread.
    finding_id = next(
        o.finding_record_id for o in occurrences if o.disposition == OccurrenceDisposition.NEW
    )
    link = await store.findings.get_thread_link(finding_id)
    assert link is not None and link.state == ThreadLinkState.LINKED


@pytest.mark.asyncio
async def test_duplicate_run_posts_no_review(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    # Run 1 creates the finding and its thread link.
    lease1, current1 = await _lease(store, head="head", delivery="d1")
    reader1 = _Reader(current1.target)
    writer1 = _Writer()
    lifecycle1 = FindingLifecycleService(
        store.findings,
        reader1,
        reader1,
        revalidator=_Revalidator(RevalidationOutcome.STILL_PRESENT),
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    exec1 = _executor(
        store,
        reader1,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        writer1,
        lifecycle1,
    )
    assert await exec1.execute(lease1, current1, now=NOW) == "completed"
    assert writer1.publish_calls == 1

    # Run 2 on a later head reports the same finding: it is an exact duplicate, so no review POST.
    lease2, current2 = await _lease(store, head="head2", delivery="d2")
    reader2 = _Reader(current2.target)
    reader2.head = "head2"
    writer2 = _Writer()
    revalidator = _Revalidator(RevalidationOutcome.STILL_PRESENT)
    lifecycle2 = FindingLifecycleService(
        store.findings,
        reader2,
        reader2,
        revalidator=revalidator,
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    exec2 = _executor(
        store,
        reader2,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        writer2,
        lifecycle2,
    )
    assert await exec2.execute(lease2, current2, now=NOW) == "completed"
    assert writer2.publish_calls == 0
    assert any("No new findings were published" in s for s in writer2.final_summaries)
    run2 = stable_id("review-run", lease2.job.id)
    occurrences = await store.findings.occurrences_for_run(run2)
    assert any(o.disposition == OccurrenceDisposition.EXACT_DUPLICATE for o in occurrences)
    # The prior finding was still selected and revalidated.
    assert revalidator.calls == 1


@pytest.mark.asyncio
async def test_fixed_prior_resolves_thread(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    lease1, current1 = await _lease(store, head="head", delivery="d1")
    reader1 = _Reader(current1.target)
    writer1 = _Writer()
    lifecycle1 = FindingLifecycleService(
        store.findings,
        reader1,
        reader1,
        revalidator=_Revalidator(RevalidationOutcome.STILL_PRESENT),
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    exec1 = _executor(
        store,
        reader1,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        writer1,
        lifecycle1,
    )
    await exec1.execute(lease1, current1, now=NOW)
    run1 = stable_id("review-run", lease1.job.id)
    finding_id = next(
        o.finding_record_id
        for o in await store.findings.occurrences_for_run(run1)
        if o.disposition == OccurrenceDisposition.NEW
    )

    # Run 2 finds no new issues; the prior finding is revalidated as fixed and its thread resolves.
    lease2, current2 = await _lease(store, head="head2", delivery="d2")
    reader2 = _Reader(current2.target)
    reader2.head = "head2"
    writer2 = _Writer()
    resolver = _ThreadResolver()
    lifecycle2 = FindingLifecycleService(
        store.findings,
        reader2,
        reader2,
        revalidator=_Revalidator(RevalidationOutcome.FIXED),
        thread_reader=_ThreadReader(),
        thread_resolver=resolver,
        marker_key=MARKER_KEY,
    )
    exec2 = _executor(
        store, reader2, _Reviewer([ReviewResult(summary="clean", findings=())]), writer2, lifecycle2
    )
    assert await exec2.execute(lease2, current2, now=NOW) == "completed"
    assert writer2.publish_calls == 0
    assert resolver.mutations == 1
    finding = await store.findings.get_finding(finding_id)
    assert finding is not None and finding.state == FindingLifecycleState.RESOLVED


@pytest.mark.asyncio
async def test_no_whole_file_content_is_persisted(alembic_database: AlembicDatabase) -> None:
    store = alembic_database.store()
    lease1, current1 = await _lease(store, head="head", delivery="d1")
    reader1 = _Reader(current1.target)
    lifecycle1 = FindingLifecycleService(
        store.findings,
        reader1,
        reader1,
        revalidator=_Revalidator(RevalidationOutcome.STILL_PRESENT),
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    exec1 = _executor(
        store,
        reader1,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        _Writer(),
        lifecycle1,
    )
    await exec1.execute(lease1, current1, now=NOW)

    # A second run fetches the whole file for revalidation; that content must stay ephemeral.
    lease2, current2 = await _lease(store, head="head2", delivery="d2")
    reader2 = _Reader(current2.target)
    reader2.head = "head2"
    lifecycle2 = FindingLifecycleService(
        store.findings,
        reader2,
        reader2,
        revalidator=_Revalidator(RevalidationOutcome.STILL_PRESENT),
        thread_reader=_ThreadReader(),
        thread_resolver=_ThreadResolver(),
        marker_key=MARKER_KEY,
    )
    exec2 = _executor(
        store,
        reader2,
        _Reviewer([ReviewResult(summary="s", findings=(_finding(),))]),
        _Writer(),
        lifecycle2,
    )
    await exec2.execute(lease2, current2, now=NOW)

    async with store.connect() as connection:
        tables = [
            row["name"]
            for row in await connection.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        haystack = ""
        for table in tables:
            rows = await connection.execute_fetchall(f"SELECT * FROM {table}")
            for row in rows:
                haystack += "\x1f".join("" if value is None else str(value) for value in row)
    assert "VENAROQ_PRIVACY_FILE_BODY_TOKEN" not in haystack
    # The bounded, validated evidence snippet is a permitted lifecycle value and is present.
    assert EVIDENCE in haystack
