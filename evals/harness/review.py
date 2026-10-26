"""Build a live, provider-backed reviewer for the harness.

This is the only part that spends tokens and talks to a provider; it is never run in CI. It composes
the real adapters and the agent loop, bypassing the durable queue, persistence, and GitHub publish
path (it calls the reviewer directly on a fixture-derived ReviewRequest). Because it bypasses
``ReviewGenerationService``, the publish-side rules that service applies are mirrored in
``published_outcome`` so the harness scores what users would actually see, not raw model output.

Infrastructure failures (credentials, rate limits, ambiguous calls) are *raised*, never scored: the
runner records them as failed runs and the report is marked incomplete.
"""

from evals.harness.cases import EvalCase, FindingLite, ReviewOutcome
from evals.harness.runner import ReviewCase
from venaroq.adapters.ai.anthropic import (
    AnthropicReviewAdapter,
    anthropic_agent_model_profile,
    anthropic_model_profile,
)
from venaroq.adapters.ai.openai import OpenAIReviewAdapter, openai_model_profile
from venaroq.application.review.agent import AgentReviewService
from venaroq.application.review.agent_tools import AgentLimits
from venaroq.application.review.execution import DiffLimiter, PreflightedReviewer
from venaroq.config.anthropic import AnthropicSettings
from venaroq.config.openai import OpenAISettings
from venaroq.config.review import ReviewSettings
from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.models import (
    ChangeRequestTarget,
    DiffCollection,
    RepositoryEntry,
    ReviewRequest,
    ReviewResult,
    TreeCollection,
)
from venaroq.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallObservedInvalidResponseError,
    ProviderCallObservedTerminalError,
)


class FixtureContentPort:
    """Serve a case's full file contents to agent mode (in-memory, read-only).

    ``get_tree`` mirrors the GitHub adapter's shape: only the direct children of ``path``, with
    sub-directories listed once as ``tree`` entries, so agent exploration matches production.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        return self._files.get(path)

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        prefix = f"{path.strip('/')}/" if path.strip("/") else ""
        blobs: list[RepositoryEntry] = []
        trees: set[str] = set()
        for name in sorted(self._files):
            if not name.startswith(prefix):
                continue
            child, _, rest = name[len(prefix) :].partition("/")
            if rest:
                trees.add(f"{prefix}{child}")
            else:
                size = len(self._files[name].encode())
                blobs.append(
                    RepositoryEntry(path=name, entry_type="blob", sha="fixture", size=size)
                )
        entries = [RepositoryEntry(path=t, entry_type="tree", sha="fixture") for t in sorted(trees)]
        return TreeCollection(items=tuple(sorted([*entries, *blobs], key=lambda e: e.path)))


def published_outcome(
    result: ReviewResult, settings: ReviewSettings, *, token_reduced: bool
) -> ReviewOutcome:
    """Apply ``ReviewGenerationService``'s publish rules to a raw provider result.

    Mirrors execution.py: drop findings below the summary-confidence threshold; if the remainder
    overflows ``review_max_findings``, the input was token-reduced, or the (agent) result is
    partial, the artifact is partial and publishes no findings.
    """
    accepted = [f for f in result.findings if f.confidence >= settings.review_summary_confidence]
    if len(accepted) > settings.review_max_findings:
        return ReviewOutcome(withheld_reason="findings_truncated")
    if token_reduced:
        return ReviewOutcome(withheld_reason="input_truncated")
    if result.partial:
        return ReviewOutcome(withheld_reason="agent_incomplete")
    return ReviewOutcome(
        findings=tuple(
            FindingLite(path=finding.path, line=finding.line, category=finding.category)
            for finding in accepted
        )
    )


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
    limiter = DiffLimiter(review_settings)
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

    async def review_case(case: EvalCase) -> ReviewOutcome:
        files, envelope = limiter.limit(
            DiffCollection(items=case.diff_files, expected_file_count=len(case.diff_files))
        )
        if envelope.partial:
            return ReviewOutcome(withheld_reason="input_incomplete")
        if not files:
            return ReviewOutcome()
        request = ReviewRequest(
            change_request=case.build_change_request(),
            diff_files=files,
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
        # Same token-admission reduction loop as ReviewGenerationService.generate.
        token_reduced = False
        while True:
            try:
                payload = await reviewer.preflight(request)
                break
            except IncompleteReviewInputError:
                if len(request.diff_files) <= 1:
                    return ReviewOutcome(withheld_reason="input_truncated")
                token_reduced = True
                request = request.model_copy(update={"diff_files": request.diff_files[:-1]})
        try:
            result = await reviewer.generate_preflighted(payload)
        except MalformedProviderOutputError:
            # Production repairs once, but even a successful repair publishes no findings.
            return ReviewOutcome(withheld_reason="output_invalid")
        except (ProviderCallObservedTerminalError, ProviderCallObservedInvalidResponseError):
            return ReviewOutcome(withheld_reason="provider_unusable_output")
        return published_outcome(result, review_settings, token_reduced=token_reduced)

    return review_case
