"""End-to-end Phase 4 job races and ambiguous-write restart behavior."""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.application.review.execution import ReviewGenerationService
from revio.application.review.job import ReviewJobExecutor, stable_id
from revio.config.review import ReviewSettings
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    Finding,
    ReviewRequest,
    ReviewResult,
)
from revio.domain.reviews import ReconciliationResult, ReviewRunState, ReviewStatusDetails
from revio.errors import (
    ProviderAnchorRejectedError,
    ProviderWriteAmbiguousError,
    ProviderWriteRejectedError,
)
from tests.conftest import AlembicDatabase


class SuccessfulReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def preflight(self, request: ReviewRequest) -> dict[str, object]:
        return {"request": request}

    async def repair_preflight(self, payload: dict[str, object]) -> dict[str, object]:
        return payload

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.calls += 1
        return ReviewResult(
            summary="Review complete.",
            findings=(
                Finding(
                    category="correctness",
                    title="Issue",
                    explanation="Explanation",
                    confidence=0.9,
                    path="a.py",
                    line=1,
                ),
            ),
        )


class RaceReader:
    def __init__(self, target: ChangeRequestTarget, *, race: bool) -> None:
        self.target = target
        self.race = race

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return ChangeRequest(
            target=target,
            title="Change",
            base_sha="base",
            head_sha="new-head" if self.race else "head",
        )

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return DiffCollection(
            items=(
                DiffFile(
                    new_path="a.py",
                    status="added",
                    lines=(DiffLine(content="ok = True", side="new", new_line=1),),
                ),
            ),
            expected_file_count=1,
        )


class AnchorRaceReader(RaceReader):
    def __init__(self, target: ChangeRequestTarget) -> None:
        super().__init__(target, race=False)
        self.change_request_calls = 0

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        self.change_request_calls += 1
        self.race = self.change_request_calls > 1
        return await super().get_change_request(target)


class RecordingWriter:
    def __init__(
        self,
        *,
        ambiguous_publish: bool = False,
        reject_first_publish: bool = False,
        ambiguous_check: bool = False,
        terminal_patch_applied: bool | None = None,
        generic_rejection: bool = False,
    ) -> None:
        self.ambiguous_publish = ambiguous_publish
        self.reject_first_publish = reject_first_publish
        self.ambiguous_check = ambiguous_check
        self.terminal_patch_applied = terminal_patch_applied
        self.generic_rejection = generic_rejection
        self.check_id: str | None = None
        self.publish_calls = 0
        self.published_findings: list[tuple[Finding, ...]] = []
        self.check_statuses: list[str] = []
        self.remote_check: dict[str, object] = {}
        self.review_id: str | None = None

    async def reconcile_check_run(
        self,
        target: ChangeRequestTarget,
        *,
        head_sha: str,
        external_id: str,
        name: str = "Revio review",
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
        self.check_statuses.append(details.status)
        if self.ambiguous_check:
            raise ProviderWriteAmbiguousError("Check Run response lost")
        self.check_id = "check-1"
        self.remote_check = {
            "status": "queued",
            "conclusion": None,
            "output": {"summary": details.summary},
        }
        return self.check_id

    async def update_check_run(
        self,
        target: ChangeRequestTarget,
        provider_id: str,
        details: ReviewStatusDetails,
    ) -> None:
        self.check_statuses.append(details.status)
        desired: dict[str, object] = {
            "status": details.status,
            "conclusion": details.conclusion,
            "output": {"summary": details.summary},
        }
        if details.status == "completed" and self.terminal_patch_applied is not None:
            if self.terminal_patch_applied:
                self.remote_check = desired
            raise ProviderWriteAmbiguousError("terminal Check Run PATCH response lost")
        self.remote_check = desired
        return None

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
    ) -> str:
        self.publish_calls += 1
        self.published_findings.append(findings)
        if self.reject_first_publish and self.publish_calls == 1:
            raise ProviderAnchorRejectedError("anchor rejected")
        if self.generic_rejection:
            raise ProviderWriteRejectedError("generic validation rejection")
        if self.ambiguous_publish:
            raise ProviderWriteAmbiguousError("response lost")
        self.review_id = "review-1"
        return self.review_id


async def _lease(store: SQLiteStore):
    normalization = normalize_webhook(
        "pull_request",
        "phase4",
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
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await store.persist(
        provider_id="github",
        delivery_identity="github:phase4",
        event_name="pull_request",
        payload_sha256="a" * 64,
        normalization=normalization,
        received_at=now,
    )
    lease = await store.lease_next("worker", now)
    assert lease is not None and lease.job.event.change_request is not None
    current = ChangeRequest(
        target=lease.job.event.change_request,
        title="Change",
        base_sha="base",
        head_sha="head",
    )
    return lease, current


def _settings() -> ReviewSettings:
    return ReviewSettings(
        environment="sandbox",
        review_enabled=True,
        review_publish_enabled=True,
        publish_marker_key=SecretStr("test-marker-key-with-sufficient-entropy"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("applied", "expected"),
    [(None, "superseded"), (True, "superseded"), (False, "check_run_indeterminate")],
)
async def test_new_head_supersedes_before_review_post(
    alembic_database: AlembicDatabase, applied: bool | None, expected: str
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reader = RaceReader(current.target, race=True)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter(terminal_patch_applied=applied)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        reader,
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    assert await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC)) == expected
    assert writer.publish_calls == 0
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("applied", "expected"),
    [(True, "superseded"), (False, "check_run_indeterminate")],
)
async def test_new_head_after_anchor_rejection_requires_confirmed_terminal_check(
    alembic_database: AlembicDatabase, applied: bool, expected: str
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    writer = RecordingWriter(reject_first_publish=True, terminal_patch_applied=applied)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        AnchorRaceReader(current.target),
        ReviewGenerationService(store.reviews, SuccessfulReviewer(), settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    assert await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC)) == expected
    assert writer.publish_calls == 1
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == expected


@pytest.mark.asyncio
async def test_successful_review_finishes_same_check_and_review_run(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reader = RaceReader(current.target, race=False)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter()
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        reader,
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    assert (
        await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC)) == "completed"
    )
    assert writer.check_statuses == ["queued", "in_progress", "completed"]
    assert writer.publish_calls == 1
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == "completed"


@pytest.mark.asyncio
async def test_ambiguous_check_create_stops_before_generation_and_never_reposts(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter(ambiguous_check=True)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        RaceReader(current.target, race=False),
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert await executor.execute(lease, current, now=now) == "check_run_indeterminate"
    assert await executor.execute(lease, current, now=now) == "check_run_indeterminate"
    assert reviewer.calls == 0
    assert writer.check_statuses == ["queued"]
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == "check_run_indeterminate"


@pytest.mark.asyncio
async def test_ambiguous_review_post_is_not_repeated_on_restart(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reader = RaceReader(current.target, race=False)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter(ambiguous_publish=True)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        reader,
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert await executor.execute(lease, current, now=now) == "publication_indeterminate"
    assert await executor.execute(lease, current, now=now) == "publication_indeterminate"
    assert reviewer.calls == 1
    assert writer.publish_calls == 1
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == "publication_indeterminate"


@pytest.mark.asyncio
async def test_only_known_rejected_anchor_uses_summary_fallback(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reader = RaceReader(current.target, race=False)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter(reject_first_publish=True)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        reader,
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    assert await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC)) == "partial"
    assert writer.publish_calls == 2
    assert len(writer.published_findings[0]) == 1
    assert writer.published_findings[1] == ()
    assert writer.check_statuses == ["queued", "in_progress", "completed"]
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("applied", "expected"),
    [(True, "completed"), (False, "check_run_indeterminate")],
)
async def test_terminal_check_patch_must_be_confirmed_before_review_run_completion(
    alembic_database: AlembicDatabase, applied: bool, expected: str
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter(terminal_patch_applied=applied)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        RaceReader(current.target, race=False),
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    outcome = await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert outcome == expected and writer.publish_calls == 1
    run = await store.reviews.get_run(stable_id("review-run", lease.job.id))
    assert run is not None and run.state == expected


@pytest.mark.asyncio
async def test_generic_validation_rejection_never_authorizes_fallback_post(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    writer = RecordingWriter(generic_rejection=True)
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        RaceReader(current.target, race=False),
        ReviewGenerationService(store.reviews, SuccessfulReviewer(), settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    assert (
        await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC))
        == "publication_indeterminate"
    )
    assert writer.publish_calls == 1


@pytest.mark.asyncio
async def test_publishing_restart_never_regresses_completed_check_or_reposts_review(
    alembic_database: AlembicDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = alembic_database.store()
    lease, current = await _lease(store)
    reviewer = SuccessfulReviewer()
    writer = RecordingWriter()
    settings = _settings()
    executor = ReviewJobExecutor(
        store.reviews,
        RaceReader(current.target, race=False),
        ReviewGenerationService(store.reviews, reviewer, settings),
        anthropic_model_profile(),
        settings,
        writer=writer,
        marker_key=b"test-marker-key-with-sufficient-entropy",
    )
    original = store.reviews.transition_run

    async def crash_before_terminal(
        run_id: str,
        expected: ReviewRunState,
        target: ReviewRunState,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> bool:
        if expected == ReviewRunState.PUBLISHING and target == ReviewRunState.COMPLETED:
            return False
        return await original(run_id, expected, target, now, reason=reason)

    monkeypatch.setattr(store.reviews, "transition_run", crash_before_terminal)
    with pytest.raises(RuntimeError, match="terminal transition"):
        await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(store.reviews, "transition_run", original)
    assert writer.remote_check["status"] == "completed"
    statuses_before = list(writer.check_statuses)
    assert (
        await executor.execute(lease, current, now=datetime(2026, 1, 1, tzinfo=UTC)) == "completed"
    )
    assert writer.publish_calls == 1 and reviewer.calls == 1
    assert writer.check_statuses == statuses_before
