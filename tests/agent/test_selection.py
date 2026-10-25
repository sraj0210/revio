"""Agent-mode gating and provenance surfacing (no provider branch in the review core)."""

from datetime import UTC, datetime

from pydantic import SecretStr

from revio.adapters.ai.anthropic import (
    AnthropicReviewAdapter,
    anthropic_agent_model_profile,
    anthropic_model_profile,
)
from revio.adapters.ai.openai import openai_model_profile
from revio.application.review.agent import AgentReviewService
from revio.application.review.execution import build_artifact
from revio.config.anthropic import AnthropicSettings
from revio.config.review import ReviewSettings
from revio.domain.capabilities import SCMCapabilities
from revio.domain.models import AgentProvenance, ConsultedFile
from revio.registries import SCMAdapterBundle
from revio.worker.main import select_reviewer
from tests.agent.test_tool_executor import FakeContent


def _adapter() -> AnthropicReviewAdapter:
    return AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("s")),
        ReviewSettings(review_enabled=True),
    )


def _bundle(*, file_access: bool = True, tree_access: bool = True) -> SCMAdapterBundle:
    class _Reader:
        pass

    return SCMAdapterBundle(
        reader=_Reader(),  # type: ignore[arg-type]
        capabilities=SCMCapabilities(repository_file_access=file_access, tree_access=tree_access),
        repository_content=FakeContent({}),  # type: ignore[arg-type]
    )


def _settings(*, agent: bool) -> ReviewSettings:
    return ReviewSettings(review_enabled=True, review_agent_mode_enabled=agent)


def test_all_gates_satisfied_selects_agent_reviewer() -> None:
    reviewer = select_reviewer(
        _adapter(), anthropic_agent_model_profile(), _settings(agent=True), _bundle()
    )
    assert isinstance(reviewer, AgentReviewService)


def test_switch_off_stays_diff_only() -> None:
    adapter = _adapter()
    reviewer = select_reviewer(
        adapter, anthropic_agent_model_profile(), _settings(agent=False), _bundle()
    )
    assert reviewer is adapter


def test_non_agent_profile_stays_diff_only() -> None:
    adapter = _adapter()
    reviewer = select_reviewer(adapter, anthropic_model_profile(), _settings(agent=True), _bundle())
    assert reviewer is adapter


def test_openai_profile_stays_diff_only() -> None:
    # OpenAI has no agent-turn adapter; even an agent switch must keep it diff-only.
    adapter = _adapter()
    reviewer = select_reviewer(adapter, openai_model_profile(), _settings(agent=True), _bundle())
    assert reviewer is adapter


def test_scm_without_file_access_stays_diff_only() -> None:
    adapter = _adapter()
    reviewer = select_reviewer(
        adapter, anthropic_agent_model_profile(), _settings(agent=True), _bundle(file_access=False)
    )
    assert reviewer is adapter


def test_no_bundle_stays_diff_only() -> None:
    adapter = _adapter()
    reviewer = select_reviewer(
        adapter, anthropic_agent_model_profile(), _settings(agent=True), None
    )
    assert reviewer is adapter


def test_provenance_note_is_surfaced_in_artifact_summary() -> None:
    provenance = AgentProvenance(
        consulted=(ConsultedFile(path="src/a.py", ref="headsha", kind="get_file"),),
        complete=True,
    )
    artifact = build_artifact(
        run_id="run-1",
        profile=anthropic_agent_model_profile(),
        summary="One issue found.",
        findings=(),
        partial=False,
        reasons=(),
        now=datetime.now(UTC),
        provenance=provenance,
    )
    assert "Verified against 1 repository file at headsha." in artifact.summary


def test_diff_only_artifact_digest_is_unchanged_without_provenance() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    without = build_artifact(
        run_id="run-2",
        profile=anthropic_model_profile(),
        summary="No issues.",
        findings=(),
        partial=False,
        reasons=(),
        now=now,
    )
    # Passing provenance=None must not change the digest versus omitting it entirely.
    also_without = build_artifact(
        run_id="run-2",
        profile=anthropic_model_profile(),
        summary="No issues.",
        findings=(),
        partial=False,
        reasons=(),
        now=now,
        provenance=None,
    )
    assert without.digest == also_without.digest
