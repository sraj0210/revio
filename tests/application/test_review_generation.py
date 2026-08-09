"""Crash/restart safety for durable Phase 4 generation."""

import uuid
from datetime import UTC, datetime
from typing import Literal

import pytest

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.execution import ReviewGenerationService, build_artifact
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
from revio.domain.reviews import (
    PartialReason,
    ProviderCallIdentity,
    ProviderCallState,
    ReviewRunState,
    UsageDisposition,
)
from revio.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallObservedInvalidResponseError,
    ProviderCallObservedTerminalError,
    ProviderCallSafeRetryError,
)
from tests.conftest import AlembicDatabase


class AmbiguousReviewer:
    def __init__(self) -> None:
        self.generations: int = 0

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


class ObservedTerminalReviewer(AmbiguousReviewer):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        raise ProviderCallObservedTerminalError(
            "observed terminal",
            usage=TokenUsage(uncached_input_tokens=11, output_tokens=3),
            reason=self.reason,
        )


class MalformedThenObservedTerminalRepairReviewer(ObservedTerminalReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        if self.generations == 1:
            raise MalformedProviderOutputError(
                "malformed", usage=TokenUsage(uncached_input_tokens=5, output_tokens=2)
            )
        raise ProviderCallObservedTerminalError(
            "observed repair terminal",
            usage=TokenUsage(uncached_input_tokens=7, output_tokens=1),
            reason=self.reason,
        )


class ObservedInvalidReviewer(AmbiguousReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        raise ProviderCallObservedInvalidResponseError("invalid observed 2xx")


class MalformedThenObservedInvalidRepairReviewer(ObservedInvalidReviewer):
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        if self.generations == 1:
            raise MalformedProviderOutputError(
                "malformed", usage=TokenUsage(uncached_input_tokens=5, output_tokens=2)
            )
        raise ProviderCallObservedInvalidResponseError("invalid observed repair 2xx")


class ReducingAdmissionReviewer(AmbiguousReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.estimates: list[int] = []

    async def preflight(self, request: ReviewRequest) -> dict[str, object]:
        estimate = len(request.diff_files) * 100
        self.estimates.append(estimate)
        if len(request.diff_files) > 1:
            raise IncompleteReviewInputError("reduce")
        return {"request": request, "_revio_estimated_input_tokens": estimate}

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        self.generations += 1
        return ReviewResult(
            summary="admitted",
            usage=TokenUsage(uncached_input_tokens=73, output_tokens=9),
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
        (1, ProviderCallState.RETRYABLE_REJECTED),
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
        (1, ProviderCallState.RETRYABLE_REJECTED),
        (2, ProviderCallState.COMPLETED),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("call_kind", ["initial", "repair"])
async def test_terminal_rejection_restart_never_advances_provider_call_ordinal(
    alembic_database: AlembicDatabase,
    change_request: ChangeRequest,
    call_kind: Literal["initial", "repair"],
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    profile = anthropic_model_profile()

    async def seed(kind: Literal["initial", "repair"], state: ProviderCallState) -> None:
        identity = ProviderCallIdentity(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"revio:provider-call:run:{kind}:1")),
            review_run_id="run",
            call_kind=kind,
            call_ordinal=1,
            provider_id=str(profile.provider_id),
            model_profile_id=profile.alias.value,
            model_profile_version=profile.profile_version,
            prompt_version="review-v1",
            schema_version="review-v1",
        )
        await store.reviews.reserve_call(identity, now)
        assert await store.reviews.transition_call(
            identity.id, ProviderCallState.RESERVED, ProviderCallState.ATTEMPT_STARTED, now
        )
        assert await store.reviews.transition_call(
            identity.id, ProviderCallState.ATTEMPT_STARTED, state, now
        )

    assert await store.reviews.transition_run(
        "run", ReviewRunState.GENERATION_PENDING, ReviewRunState.GENERATION_ATTEMPTED, now
    )
    if call_kind == "repair":
        await seed("initial", ProviderCallState.RESPONSE_OBSERVED)
        initial = await store.reviews.latest_call("run", "initial")
        assert initial is not None
        assert await store.reviews.transition_call(
            initial.identity.id,
            ProviderCallState.RESPONSE_OBSERVED,
            ProviderCallState.COMPLETED,
            now,
        )
    await seed(call_kind, ProviderCallState.TERMINAL_REJECTED)

    reviewer = AmbiguousReviewer()
    artifact = await ReviewGenerationService(
        store.reviews, reviewer, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=profile,
        now=now,
    )
    assert artifact.partial and reviewer.generations == 0
    latest = await store.reviews.latest_call("run", call_kind)
    assert latest is not None
    assert latest.identity.call_ordinal == 1
    assert latest.state == ProviderCallState.TERMINAL_REJECTED


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "partial_reason"),
    [
        ("refusal", PartialReason.PROVIDER_REFUSAL),
        ("max_tokens", PartialReason.PROVIDER_OUTPUT_TRUNCATED),
    ],
)
async def test_observed_terminal_response_keeps_usage_and_never_advances_ordinal(
    alembic_database: AlembicDatabase,
    change_request: ChangeRequest,
    reason: str,
    partial_reason: PartialReason,
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = ObservedTerminalReviewer(reason)
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    first = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    second = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert first == second and first.reason_codes == (partial_reason,)
    assert reviewer.generations == 1
    async with store.connect() as connection:
        calls = await connection.execute_fetchall(
            "SELECT call_ordinal, state FROM provider_calls ORDER BY call_ordinal"
        )
        usage = await connection.execute_fetchall(
            "SELECT usage_status, uncached_input_tokens, output_tokens FROM provider_usage"
        )
    assert [tuple(row) for row in calls] == [(1, "completed")]
    assert [tuple(row) for row in usage] == [("known", 11, 3)]


@pytest.mark.asyncio
async def test_restart_after_response_observed_completes_same_call_without_messages(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    identity = ProviderCallIdentity(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, "revio:provider-call:run:initial:1")),
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
    assert await store.reviews.transition_call(
        identity.id, ProviderCallState.RESERVED, ProviderCallState.ATTEMPT_STARTED, now
    )
    assert await store.reviews.transition_call(
        identity.id,
        ProviderCallState.ATTEMPT_STARTED,
        ProviderCallState.RESPONSE_OBSERVED,
        now,
        usage=UsageDisposition(status="known", usage=TokenUsage(output_tokens=1)),
    )
    reviewer = FindingsReviewer(())
    artifact = await ReviewGenerationService(
        store.reviews, reviewer, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert artifact.reason_codes == (PartialReason.RESPONSE_RECOVERY_UNAVAILABLE,)
    assert reviewer.generations == 0
    call = await store.reviews.get_call(identity.id)
    assert call is not None and call.state == ProviderCallState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reviewer", "call_kind"),
    [
        (ObservedInvalidReviewer(), "initial"),
        (MalformedThenObservedInvalidRepairReviewer(), "repair"),
    ],
)
async def test_observed_invalid_response_is_unknown_usage_and_never_retransmitted(
    alembic_database: AlembicDatabase,
    change_request: ChangeRequest,
    reviewer: AmbiguousReviewer,
    call_kind: Literal["initial", "repair"],
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    service = ReviewGenerationService(store.reviews, reviewer, ReviewSettings(review_enabled=True))
    first = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    generation_count = reviewer.generations
    second = await service.generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert first == second and reviewer.generations == generation_count
    latest = await store.reviews.latest_call("run", call_kind)
    assert latest is not None and latest.identity.call_ordinal == 1
    assert latest.state == ProviderCallState.COMPLETED
    async with store.connect() as connection:
        usage = await connection.execute_fetchall(
            "SELECT usage_status FROM provider_usage WHERE provider_call_id = ?",
            (latest.identity.id,),
        )
    assert [row["usage_status"] for row in usage] == ["unknown"]


@pytest.mark.asyncio
async def test_observed_terminal_repair_keeps_usage_and_completes(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = MalformedThenObservedTerminalRepairReviewer("refusal")
    artifact = await ReviewGenerationService(
        store.reviews, reviewer, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert artifact.partial and reviewer.generations == 2
    async with store.connect() as connection:
        rows = await connection.execute_fetchall(
            "SELECT call_kind, state FROM provider_calls ORDER BY call_kind"
        )
        usage = await connection.execute_fetchall(
            "SELECT usage_status, output_tokens FROM provider_usage ORDER BY provider_call_id"
        )
    assert [tuple(row) for row in rows] == [
        ("initial", "completed"),
        ("repair", "completed"),
    ]
    assert all(row[0] == "known" for row in usage)


@pytest.mark.asyncio
async def test_restart_after_artifact_persistence_finalizes_observed_call(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    identity = ProviderCallIdentity(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, "revio:provider-call:run:initial:1")),
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
    assert await store.reviews.transition_call(
        identity.id, ProviderCallState.RESERVED, ProviderCallState.ATTEMPT_STARTED, now
    )
    assert await store.reviews.transition_run(
        "run", ReviewRunState.GENERATION_PENDING, ReviewRunState.GENERATION_ATTEMPTED, now
    )
    assert await store.reviews.transition_call(
        identity.id,
        ProviderCallState.ATTEMPT_STARTED,
        ProviderCallState.RESPONSE_OBSERVED,
        now,
        usage=UsageDisposition(status="known", usage=TokenUsage(output_tokens=1)),
    )
    durable = build_artifact(
        run_id="run",
        profile=anthropic_model_profile(),
        summary="AI review generation did not produce a usable review.",
        findings=(),
        partial=True,
        reasons=(PartialReason.PROVIDER_REFUSAL,),
        now=now,
    )
    assert await store.reviews.persist_artifact(durable, ReviewRunState.GENERATION_ATTEMPTED)
    reviewer = FindingsReviewer(())
    recovered = await ReviewGenerationService(
        store.reviews, reviewer, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_model_profile(),
        now=now,
    )
    assert recovered == durable and reviewer.generations == 0
    call = await store.reviews.get_call(identity.id)
    assert call is not None and call.state == ProviderCallState.COMPLETED


@pytest.mark.asyncio
async def test_final_token_estimate_is_durable_and_separate_from_billed_usage(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _run(store, now)
    reviewer = ReducingAdmissionReviewer()
    diff = DiffCollection(
        items=tuple(
            DiffFile(
                new_path=path,
                status="added",
                lines=(DiffLine(content="x = 1", side="new", new_line=1),),
            )
            for path in ("a.py", "b.py")
        ),
        expected_file_count=2,
    )
    artifact = await ReviewGenerationService(
        store.reviews, reviewer, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=diff,
        profile=anthropic_model_profile(),
        now=now,
    )
    assert artifact.partial and reviewer.estimates == [200, 100]
    async with store.connect() as connection:
        call = await connection.execute_fetchall(
            "SELECT estimated_input_tokens FROM provider_calls"
        )
        usage = await connection.execute_fetchall(
            "SELECT uncached_input_tokens FROM provider_usage"
        )
    assert [tuple(row) for row in call] == [(100,)]
    assert [tuple(row) for row in usage] == [(73,)]
