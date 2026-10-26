"""The provider-neutral agent-review loop.

``AgentReviewService`` implements the same preflight/generate/repair surface as the diff-only
reviewer, so the durable ProviderCall state machine and artifact persistence in
``ReviewGenerationService`` are reused unchanged. Internally it drives a bounded tool loop: it asks
the adapter for one model turn, executes any requested tools through the guarded core executor,
feeds results back, and repeats until the model returns a final review or a limit is reached.
Tool execution has no external side effects, but a non-converging loop yields a partial
review rather than an unbounded crawl, and an ambiguous turn still aborts (never retransmits) via
the shared provider-call taxonomy raised by the adapter.
"""

from venaroq.application.review.agent_tools import AgentLimits, AgentToolExecutor
from venaroq.application.review.execution import contains_long_verbatim_source
from venaroq.domain.models import (
    ChangeRequestTarget,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
)
from venaroq.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallObservedTerminalError,
)
from venaroq.ports.ai import AgentReviewGenerator
from venaroq.ports.scm import RepositoryContentReadPort

_NO_CONVERGENCE_SUMMARY = (
    "Agent review did not converge within its step budget; no findings were published."
)
_QUOTED_CONTENT_SUMMARY = (
    "Agent review was withheld because its output reproduced fetched repository content."
)


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(
        uncached_input_tokens=a.uncached_input_tokens + b.uncached_input_tokens,
        cached_input_tokens=a.cached_input_tokens + b.cached_input_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


def _quotes_fetched_content(result: ReviewResult, fetched: tuple[str, ...]) -> bool:
    """True if the published prose reproduces a long verbatim span from any fetched file.

    This enforces verification-only: the model may read files to judge findings but must not copy
    their contents into the summary or finding narrative, closing the main exfiltration channel.
    """
    if not fetched:
        return False
    prose = (
        result.summary,
        *(finding.title for finding in result.findings),
        *(finding.explanation for finding in result.findings),
    )
    return contains_long_verbatim_source(prose, fetched)


class AgentReviewService:
    def __init__(
        self,
        adapter: AgentReviewGenerator,
        content: RepositoryContentReadPort,
        *,
        limits: AgentLimits,
        max_turns: int,
    ) -> None:
        self._adapter = adapter
        self._content = content
        self._limits = limits
        self._max_turns = max_turns

    async def preflight(self, request: ReviewRequest) -> dict[str, object]:
        payload = await self._adapter.agent_preflight(request)
        payload["_venaroq_agent_target"] = request.change_request.target
        payload["_venaroq_agent_ref"] = request.change_request.head_sha
        return payload

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        target = payload.get("_venaroq_agent_target")
        ref = payload.get("_venaroq_agent_ref")
        if not isinstance(target, ChangeRequestTarget) or not isinstance(ref, str):
            raise RuntimeError("agent preflight payload is missing its repository target")
        executor = AgentToolExecutor(self._content, target, ref, self._limits)
        total = TokenUsage()
        state = payload
        for _ in range(self._max_turns):
            try:
                turn = await self._adapter.agent_turn(state)
            except ProviderCallObservedTerminalError as error:
                # Fold prior turns' tokens into observed usage so accounting is not undercounted.
                observed = error.usage if isinstance(error.usage, TokenUsage) else TokenUsage()
                raise ProviderCallObservedTerminalError(
                    str(error), usage=_add_usage(total, observed), reason=error.reason
                ) from None
            except MalformedProviderOutputError as error:
                observed = error.usage if isinstance(error.usage, TokenUsage) else TokenUsage()
                raise MalformedProviderOutputError(
                    str(error), usage=_add_usage(total, observed), detail=error.detail
                ) from None
            total = _add_usage(total, turn.usage)
            if turn.final_review is not None:
                # A final review stands even if context was limited; the provenance note records
                # that. Findings are kept (verification-only mode never drops on limited context).
                review = turn.final_review.model_copy(
                    update={"usage": total, "provenance": executor.provenance()}
                )
                if _quotes_fetched_content(review, executor.fetched_contents()):
                    # The model copied fetched file content into its output: withhold it entirely
                    # rather than publish a potential data-exfiltration channel.
                    return ReviewResult(
                        findings=(),
                        summary=_QUOTED_CONTENT_SUMMARY,
                        usage=total,
                        partial=True,
                        provenance=executor.provenance().model_copy(update={"complete": False}),
                    )
                return review
            if not turn.tool_calls:
                raise MalformedProviderOutputError(
                    "agent turn produced neither tools nor a review",
                    usage=total,
                    detail="agent_no_tools_or_review",
                )
            results = await executor.execute(turn.tool_calls)
            state = self._adapter.append_tool_results(turn.payload, results)
        provenance = executor.provenance().model_copy(update={"complete": False})
        return ReviewResult(
            findings=(),
            summary=_NO_CONVERGENCE_SUMMARY,
            usage=total,
            partial=True,
            provenance=provenance,
        )

    async def repair_preflight(self, payload: dict[str, object]) -> dict[str, object]:
        # Agent mode does not issue a separate structured-output repair call; a malformed final turn
        # degrades to a partial review via the shared repair-failed fallback.
        raise IncompleteReviewInputError("agent review does not support structured-output repair")
