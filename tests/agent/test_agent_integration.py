"""End-to-end: ReviewGenerationService.generate driven by a real AgentReviewService.

This exercises the integration seam the unit tests do not: the durable generate() state machine with
an agent reviewer plugged in, including provenance surfacing into the persisted artifact and the
non-convergence partial path.
"""

from datetime import UTC, datetime

import pytest

from revio.adapters.ai.anthropic import anthropic_agent_model_profile
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.application.review.agent import AgentReviewService
from revio.application.review.agent_tools import AgentLimits
from revio.application.review.execution import ReviewGenerationService
from revio.config.review import ReviewSettings
from revio.domain.agent import AgentToolRequest, AgentTurnResult
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    DiffLine,
    ReviewResult,
    TokenUsage,
)
from revio.domain.reviews import PartialReason
from tests.agent.test_agent_service import FakeAgentAdapter, FakeContent, make_finding
from tests.conftest import AlembicDatabase


async def _seed_run(store: SQLiteStore, now: datetime) -> None:
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


def _diff() -> DiffCollection:
    return DiffCollection(
        items=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        expected_file_count=1,
    )


def _service(store: SQLiteStore, adapter: FakeAgentAdapter) -> ReviewGenerationService:
    agent = AgentReviewService(
        adapter, FakeContent({"a.py": "def f(): return 1"}), limits=AgentLimits(), max_turns=4
    )
    return ReviewGenerationService(store.reviews, agent, ReviewSettings(review_enabled=True))


@pytest.mark.asyncio
async def test_converged_agent_run_persists_finding_and_provenance(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _seed_run(store, now)
    adapter = FakeAgentAdapter(
        [
            AgentTurnResult(
                tool_calls=(
                    AgentToolRequest(call_id="t1", name="get_file", arguments={"path": "a.py"}),
                ),
                usage=TokenUsage(output_tokens=1),
                payload={"messages": []},
            ),
            AgentTurnResult(
                final_review=ReviewResult(
                    findings=(make_finding(),), summary="Verified one issue."
                ),
                usage=TokenUsage(output_tokens=2),
            ),
        ]
    )
    artifact = await _service(store, adapter).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_agent_model_profile(),
        now=now,
    )
    assert artifact.partial is False
    assert len(artifact.findings) == 1
    assert "Verified against 1 repository file at head." in artifact.summary
    # Restart is a no-op: the committed artifact is returned unchanged.
    again = await _service(store, FakeAgentAdapter([])).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_agent_model_profile(),
        now=now,
    )
    assert again == artifact


@pytest.mark.asyncio
async def test_non_converged_agent_run_persists_partial(
    alembic_database: AlembicDatabase, change_request: ChangeRequest
) -> None:
    store = alembic_database.store()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    await _seed_run(store, now)
    loop_turn = AgentTurnResult(
        tool_calls=(AgentToolRequest(call_id="t", name="get_tree", arguments={"path": ""}),),
        usage=TokenUsage(output_tokens=1),
        payload={"messages": []},
    )
    agent = AgentReviewService(
        FakeAgentAdapter([loop_turn] * 10), FakeContent({}), limits=AgentLimits(), max_turns=3
    )
    artifact = await ReviewGenerationService(
        store.reviews, agent, ReviewSettings(review_enabled=True)
    ).generate(
        run_id="run",
        change_request=change_request,
        diff=_diff(),
        profile=anthropic_agent_model_profile(),
        now=now,
    )
    assert artifact.partial is True
    assert PartialReason.AGENT_INCOMPLETE in artifact.reason_codes
    assert artifact.findings == ()
