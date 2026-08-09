"""Crash/restart safety for durable Phase 4 generation."""

from datetime import UTC, datetime

import pytest

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.execution import ReviewGenerationService
from revio.config.review import ReviewSettings
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    Finding,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
)
from revio.domain.reviews import PartialReason, ProviderCallState
from revio.errors import (
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallSafeRetryError,
)
from tests.conftest import AlembicDatabase


class AmbiguousReviewer:
    def __init__(self) -> None:
        self.generations = 0

    async def preflight(self, request: ReviewRequest) -> dict[str, object]:
        return {"request": request}

    async def repair_preflight(self, payload: dict[str, object]) -> dict[str, object]:
        return payload

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        raise ProviderCallAmbiguousError("lost")


class MalformedThenAmbiguousRepairReviewer(AmbiguousReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        if self.generations == 1:
            raise MalformedProviderOutputError("malformed", usage=TokenUsage(output_tokens=1))
        raise ProviderCallAmbiguousError("repair lost")


class RetryThenSuccessReviewer(AmbiguousReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        if self.generations == 1:
            raise ProviderCallSafeRetryError("known rejection")
        return ReviewResult(summary="ok")


class MalformedRepairRetrySuccessReviewer(AmbiguousReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        if self.generations == 1:
            raise MalformedProviderOutputError("malformed", usage=TokenUsage(output_tokens=1))
        if self.generations == 2:
            raise ProviderCallSafeRetryError("known repair rejection")
        return ReviewResult(summary="repaired")


class FindingsReviewer(AmbiguousReviewer):
    def __init__(self, confidences: tuple[float, ...]) -> None:
        super().__init__()
        self.confidences = confidences

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        return ReviewResult(
            summary="routed",
            findings=tuple(
                Finding(
                    category="correctness",
                    title=f"finding-{index}",
                    explanation="explanation",
                    confidence=confidence,
                    path="a.py",
                    line=1,
                )
                for index, confidence in enumerate(self.confidences)
            ),
        )


async def _run(store: SQLiteStore, now: datetime) -> None:
    async with store.connect() as connection:
        stamp = now.isoformat()
        await connection.execute(
            "INSERT INTO queue_jobs (id,provider_id,job_type,semantic_identity,event_json,state,"
            "attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES ('job','github','review','semantic','{}','pending',0,5,?,?,?)",
            (stamp, stamp, stamp),
        )
        await connection.execute(
            "INSERT INTO review_runs (id,job_id,state,validated_head_sha,validated_base_sha,"
            "check_run_external_id,created_at,updated_at) "
            "VALUES ('run','job','generation_pending','head','base','external',?,?)",
            (stamp, stamp),
        )
        await connection.commit()


@pytest.mark.asyncio
async def test_ambiguous_generation_persists_artifact_and_restart_does_not_repeat(
    alembic_database: AlembicDatabase,
    change_request: ChangeRequest,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = AmbiguousReviewer()
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    diff = DiffCollection(
        items=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        expected_file_count=1,
    )
    first = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=diff,
        profile=anthropic_model_profile(),
        now=now,
    )
    second = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=diff,
        profile=anthropic_model_profile(),
        now=now,
    )
    assert first == second
    assert first.reason_codes == (PartialReason.PROVIDER_CALL_AMBIGUOUS,)
    assert reviewer.generations == 1


@pytest.mark.asyncio
async def test_ambiguous_repair_is_never_retransmitted_after_restart(
    alembic_database: AlembicDatabase,
    change_request: ChangeRequest,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = MalformedThenAmbiguousRepairReviewer()
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    diff = DiffCollection(
        items=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        expected_file_count=1,
    )
    first = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=diff,
        profile=anthropic_model_profile(),
        now=now,
    )
    second = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=diff,
        profile=anthropic_model_profile(),
        now=now,
    )
    assert first == second
    assert first.reason_codes == (PartialReason.REPAIR_FAILED,)
    assert reviewer.generations == 2
    async with store.connect() as connection:
        rows = await (
            await connection.execute(
                "SELECT call_kind, state FROM provider_calls ORDER BY call_kind"
            )
        ).fetchall()
    assert [(row["call_kind"], row["state"]) for row in rows] == [
        ("initial", ProviderCallState.COMPLETED),
        ("repair", ProviderCallState.AMBIGUOUS),
    ]


def _diff() -> DiffCollection:
    return DiffCollection(
        items=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="value = 1", side="new", new_line=1),),
            ),
        ),
        expected_file_count=1,
    )


@pytest.mark.asyncio
async def test_safe_initial_retry_uses_next_provider_call_ordinal(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = RetryThenSuccessReviewer()
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    with pytest.raises(ProviderCallSafeRetryError):
        await service.generate(
            run_id="run",
            change_request=change_request,
            diff=_diff(),
            profile=anthropic_model_profile(),
            now=now,
        )
    artifact = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert not artifact.partial and reviewer.generations == 2
    async with store.connect() as connection:
        rows = await (
            await connection.execute(
                "SELECT call_ordinal, state FROM provider_calls WHERE call_kind='initial' "
                "ORDER BY call_ordinal"
            )
        ).fetchall()
    assert [(row["call_ordinal"], row["state"]) for row in rows] == [
        (1, ProviderCallState.KNOWN_REJECTED),
        (2, ProviderCallState.COMPLETED),
    ]


@pytest.mark.asyncio
async def test_safe_repair_retry_uses_independent_next_ordinal(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = MalformedRepairRetrySuccessReviewer()
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    with pytest.raises(ProviderCallSafeRetryError):
        await service.generate(
            run_id="run",
            change_request=change_request,
            diff=_diff(),
            profile=anthropic_model_profile(),
            now=now,
        )
    artifact = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert artifact.partial and reviewer.generations == 3
    async with store.connect() as connection:
        rows = await (
            await connection.execute(
                "SELECT call_ordinal, state FROM provider_calls WHERE call_kind='repair' "
                "ORDER BY call_ordinal"
            )
        ).fetchall()
    assert [(row["call_ordinal"], row["state"]) for row in rows] == [
        (1, ProviderCallState.KNOWN_REJECTED),
        (2, ProviderCallState.COMPLETED),
    ]


@pytest.mark.asyncio
async def test_confidence_bands_and_overflow_are_explicit(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = FindingsReviewer((0.95, 0.80, 0.79, 0.60, 0.59))
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    artifact = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert [finding.confidence for finding in artifact.findings] == [0.95, 0.80, 0.79, 0.60]
    assert [finding.inline_eligible for finding in artifact.findings] == [True, True, False, False]


@pytest.mark.asyncio
async def test_findings_overflow_is_partial_summary_only(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = FindingsReviewer((0.9, 0.9, 0.9))
    settings = ReviewSettings(review_enabled=True, review_max_findings=2)
    artifact = await ReviewGenerationService(store.reviews, reviewer, settings).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert artifact.partial and artifact.findings == ()
    assert artifact.reason_codes == (PartialReason.FINDINGS_TRUNCATED,)


@pytest.mark.asyncio
async def test_custom_confidence_thresholds_control_routing(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = FindingsReviewer((0.69, 0.70, 0.89, 0.90))
    settings = ReviewSettings(
        review_enabled=True,
        review_summary_confidence=0.70,
        review_inline_confidence=0.90,
    )
    artifact = await ReviewGenerationService(store.reviews, reviewer, settings).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert [finding.confidence for finding in artifact.findings] == [0.70, 0.89, 0.90]
    assert [finding.inline_eligible for finding in artifact.findings] == [False, False, True]
