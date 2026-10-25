"""Build a live, provider-backed reviewer for the harness.

This is the only part that spends tokens and talks to a provider; it is never run in CI. It composes
the real adapters and the agent loop exactly as production does, bypassing only the durable queue
and GitHub publish path (it calls the reviewer directly on a fixture-derived ReviewRequest).
"""

from collections.abc import Sequence

from evals.harness.cases import EvalCase, FindingLite
from evals.harness.runner import ReviewCase
from venaroq.adapters.ai.anthropic import (
    AnthropicReviewAdapter,
    anthropic_agent_model_profile,
    anthropic_model_profile,
)
from venaroq.adapters.ai.openai import OpenAIReviewAdapter, openai_model_profile
from venaroq.application.review.agent import AgentReviewService
from venaroq.application.review.agent_tools import AgentLimits
from venaroq.application.review.execution import PreflightedReviewer
from venaroq.config.anthropic import AnthropicSettings
from venaroq.config.openai import OpenAISettings
from venaroq.config.review import ReviewSettings
from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.models import (
    ChangeRequestTarget,
    RepositoryEntry,
    ReviewRequest,
    TreeCollection,
)


class FixtureContentPort:
    """Serve a case's full file contents to agent mode (in-memory, read-only)."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        return self._files.get(path)

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        prefix = f"{path}/" if path else ""
        entries = tuple(
            RepositoryEntry(path=name, entry_type="blob", sha="fixture")
            for name in sorted(self._files)
            if name.startswith(prefix)
        )
        return TreeCollection(items=entries)


def _profile(provider: str, mode: str) -> ResolvedModelProfile:
    if provider == "anthropic":
        return anthropic_agent_model_profile() if mode == "agent" else anthropic_model_profile()
    if provider == "openai":
        if mode == "agent":
            raise ValueError("OpenAI has no agent-turn adapter; use --mode diff-only")
        return openai_model_profile()
    raise ValueError(f"unknown provider {provider!r}")


def build_review_case(provider: str, mode: str) -> ReviewCase:
    review_settings = ReviewSettings()
    profile = _profile(provider, mode)
    adapter: AnthropicReviewAdapter | OpenAIReviewAdapter
    if provider == "anthropic":
        adapter = AnthropicReviewAdapter(AnthropicSettings(), review_settings)
    else:
        adapter = OpenAIReviewAdapter(OpenAISettings(), review_settings)
    limits = AgentLimits(
        max_tool_calls=review_settings.review_agent_max_tool_calls,
        max_files=review_settings.review_agent_max_files,
        max_bytes_per_file=review_settings.review_agent_max_bytes_per_file,
        max_aggregate_bytes=review_settings.review_agent_max_aggregate_bytes,
        max_tree_entries=review_settings.review_agent_max_tree_entries,
    )

    async def review_case(case: EvalCase) -> Sequence[FindingLite]:
        request = ReviewRequest(
            change_request=case.build_change_request(),
            diff_files=case.diff_files,
            model_profile=profile,
        )
        reviewer: PreflightedReviewer
        if mode == "agent":
            # Agent mode is Anthropic-only (enforced in _profile); narrow for the agent-turn port.
            assert isinstance(adapter, AnthropicReviewAdapter)
            reviewer = AgentReviewService(
                adapter,
                FixtureContentPort(case.repository_files),
                limits=limits,
                max_turns=review_settings.review_agent_max_turns,
            )
        else:
            reviewer = adapter
        try:
            result = await reviewer.generate_preflighted(await reviewer.preflight(request))
        except Exception as error:  # eval robustness: a failed review scores as "found nothing"
            print(f"  ! case {case.id}: review failed ({type(error).__name__}: {error})")
            return ()
        return [
            FindingLite(path=finding.path, line=finding.line, category=finding.category)
            for finding in result.findings
        ]

    return review_case
