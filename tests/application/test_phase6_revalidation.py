"""Ambiguity-safe revalidation ProviderCall driver matrix."""

from datetime import UTC, datetime

import pytest

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.lifecycle import FindingLifecycleService
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
    RevalidationCallState,
    RevalidationOutcome,
)
from revio.domain.models import ChangeRequest, DiffCollection, TokenUsage, TreeCollection
from revio.domain.revalidation import (
    FindingRevalidationBatchResult,
    FindingRevalidationRequest,
    RevalidationResultItem,
)
from revio.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallSafeRetryError,
)
from tests.conftest import AlembicDatabase

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DIGEST = "a" * 64
SECONDARY = "c" * 64


def _target() -> ChangeRequestTarget:
    provider = ProviderId(value="anthropic")
    installation = InstallationRef(provider_id=provider, external_id="174693302")
    repository = RepositoryRef(installation=installation, external_id="1328857147")
    return ChangeRequestTarget(repository=repository, external_number=7)


def _cr(head: str = "head") -> ChangeRequest:
    return ChangeRequest(target=_target(), title="t", base_sha="base", head_sha=head)


def _policy() -> EffectiveReviewPolicy:
    return EffectiveReviewPolicy(summary_confidence=0.6, inline_confidence=0.8)


def _record(index: int) -> FindingRecord:
    return FindingRecord(
        id=f"finding-{index}",
        provider_id="anthropic",
        installation_id="174693302",
        repository_id="1328857147",
        external_number=7,
        fingerprint_version="finding-v1",
        primary_fingerprint=f"{index:064x}",
        secondary_fingerprint=SECONDARY,
        path=f"src/f{index}.py",
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
        created_at=NOW,
        updated_at=NOW,
    )


class _Reader:
    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        return _cr()

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        return DiffCollection()


class _Content:
    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        return "def f():\n    return 1\n"

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise NotImplementedError


class _Revalidator:
    def __init__(
        self,
        *,
        outcomes: dict[str, RevalidationOutcome] | None = None,
        error: Exception | None = None,
        max_items: int = 1000,
        drop_id: str | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.error = error
        self.max_items = max_items
        self.drop_id = drop_id
        self.generations = 0

    async def preflight_revalidation(
        self, request: FindingRevalidationRequest
    ) -> dict[str, object]:
        if len(request.items) > self.max_items:
            raise IncompleteReviewInputError("too large")
        return {
            "_revio_estimated_input_tokens": 10,
            "_ids": [item.finding_id for item in request.items],
        }

    async def generate_revalidation(
        self, payload: dict[str, object]
    ) -> FindingRevalidationBatchResult:
        self.generations += 1
        if self.error is not None:
            raise self.error
        ids = [i for i in payload["_ids"] if isinstance(i, str)]  # type: ignore[union-attr]
        results = tuple(
            RevalidationResultItem(
                finding_id=fid,
                outcome=self.outcomes.get(fid, RevalidationOutcome.STILL_PRESENT),
            )
            for fid in ids
            if fid != self.drop_id
        )
        return FindingRevalidationBatchResult(results=results, usage=TokenUsage(output_tokens=3))


async def _setup(store: SQLiteStore, count: int) -> None:
    async with store.connect() as connection:
        stamp = NOW.isoformat()
        for job in ("prior-job", "job"):
            await connection.execute(
                "INSERT INTO queue_jobs (id,provider_id,job_type,semantic_identity,event_json,"
                "state,attempt_count,max_attempts,available_at,created_at,updated_at) "
                "VALUES (?,'anthropic','review',?,'{}','pending',0,5,?,?,?)",
                (job, job, stamp, stamp, stamp),
            )
        await connection.commit()
    for run_id, job_id in (("prior-run", "prior-job"), ("run", "job")):
        await store.reviews.create_run(
            run_id=run_id,
            job_id=job_id,
            head_sha="head",
            base_sha="base",
            external_id=f"ext-{run_id}",
            now=NOW,
        )
    for index in range(count):
        await store.findings.upsert_finding(_record(index), NOW)


def _service(store: SQLiteStore, revalidator: _Revalidator | None) -> FindingLifecycleService:
    return FindingLifecycleService(store.findings, _Reader(), _Content(), revalidator=revalidator)


@pytest.mark.asyncio
async def test_revalidation_success_persists_outcomes(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 2)
    reval = _Revalidator(outcomes={"finding-0": RevalidationOutcome.FIXED})
    service = _service(store, reval)
    superseded = await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    assert superseded is False
    results = {r.finding_record_id: r for r in await store.findings.revalidation_results("run")}
    assert results["finding-0"].outcome == RevalidationOutcome.FIXED
    assert results["finding-1"].outcome == RevalidationOutcome.STILL_PRESENT
    call = await store.findings.latest_revalidation_call("run")
    assert call is not None and call.state == RevalidationCallState.COMPLETED
    usage = await store.findings.revalidation_usage("run")
    assert usage is not None and usage.status == "known"
    assert reval.generations == 1


@pytest.mark.asyncio
async def test_revalidation_ambiguous_is_not_retransmitted(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 1)
    reval = _Revalidator(error=ProviderCallAmbiguousError("lost"))
    service = _service(store, reval)
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    results = await store.findings.revalidation_results("run")
    assert all(r.outcome == RevalidationOutcome.UNCERTAIN for r in results)
    call = await store.findings.latest_revalidation_call("run")
    assert call is not None and call.state == RevalidationCallState.AMBIGUOUS
    usage = await store.findings.revalidation_usage("run")
    assert usage is not None and usage.status == "unknown"
    # Restart: the ambiguous call must never retransmit.
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    assert reval.generations == 1


@pytest.mark.asyncio
async def test_revalidation_malformed_ids_make_batch_uncertain(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 2)
    reval = _Revalidator(drop_id="finding-1")  # omit one requested id -> incomplete batch
    service = _service(store, reval)
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    results = await store.findings.revalidation_results("run")
    assert {r.outcome for r in results} == {RevalidationOutcome.UNCERTAIN}


@pytest.mark.asyncio
async def test_revalidation_token_limit_drops_suffix(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 3)
    reval = _Revalidator(outcomes={}, max_items=2)  # third item must be dropped
    service = _service(store, reval)
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    results = {r.finding_record_id: r for r in await store.findings.revalidation_results("run")}
    dropped = [r for r in results.values() if r.reason_code == "revalidation_limit"]
    assert len(dropped) == 1


@pytest.mark.asyncio
async def test_revalidation_safe_retry_reraises_and_creates_new_ordinal(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 1)
    reval = _Revalidator(error=ProviderCallSafeRetryError("429"))
    service = _service(store, reval)
    with pytest.raises(ProviderCallSafeRetryError):
        await service.run_revalidation(
            run_id="run",
            current=_cr(),
            diff=DiffCollection(),
            policy=_policy(),
            profile=anthropic_model_profile(),
            now=NOW,
        )
    call = await store.findings.latest_revalidation_call("run")
    assert call is not None and call.state == RevalidationCallState.RETRYABLE_REJECTED
    assert call.identity.batch_ordinal == 1
    # A later attempt that succeeds creates the next ordinal.
    reval.error = None
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    call = await store.findings.latest_revalidation_call("run")
    assert call is not None and call.identity.batch_ordinal == 2
    assert call.state == RevalidationCallState.COMPLETED


@pytest.mark.asyncio
async def test_revalidation_malformed_output_marks_uncertain(
    alembic_database: AlembicDatabase,
) -> None:
    store = alembic_database.store()
    await _setup(store, 1)
    reval = _Revalidator(
        error=MalformedProviderOutputError("bad", usage=TokenUsage(output_tokens=1))
    )
    service = _service(store, reval)
    await service.run_revalidation(
        run_id="run",
        current=_cr(),
        diff=DiffCollection(),
        policy=_policy(),
        profile=anthropic_model_profile(),
        now=NOW,
    )
    results = await store.findings.revalidation_results("run")
    assert all(r.outcome == RevalidationOutcome.UNCERTAIN for r in results)
    call = await store.findings.latest_revalidation_call("run")
    assert call is not None and call.state == RevalidationCallState.COMPLETED
