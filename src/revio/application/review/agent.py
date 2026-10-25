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

from revio.application.review.agent_tools import AgentLimits, AgentToolExecutor
from revio.domain.models import (
    ChangeRequestTarget,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
)
from revio.errors import IncompleteReviewInputError, MalformedProviderOutputError
from revio.ports.ai import AgentReviewGenerator
from revio.ports.scm import RepositoryContentReadPort

_NO_CONVERGENCE_SUMMARY = (
    "Agent review did not converge within its step budget; no findings were published."
)


def _add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(
        uncached_input_tokens=a.uncached_input_tokens + b.uncached_input_tokens,
        cached_input_tokens=a.cached_input_tokens + b.cached_input_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
    )


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
        payload["_revio_agent_target"] = request.change_request.target
        payload["_revio_agent_ref"] = request.change_request.head_sha
        return payload

    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult:
        target = payload.get("_revio_agent_target")
        ref = payload.get("_revio_agent_ref")
        if not isinstance(target, ChangeRequestTarget) or not isinstance(ref, str):
            raise RuntimeError("agent preflight payload is missing its repository target")
        executor = AgentToolExecutor(self._content, target, ref, self._limits)
        total = TokenUsage()
        state = payload
        for _ in range(self._max_turns):
            turn = await self._adapter.agent_turn(state)
            total = _add_usage(total, turn.usage)
            if turn.final_review is not None:
                # A final review stands even if context was limited; the provenance note records
                # that. Findings are kept (verification-only mode never drops on limited context).
                return turn.final_review.model_copy(
                    update={"usage": total, "provenance": executor.provenance()}
                )
            if not turn.tool_calls:
                raise MalformedProviderOutputError(
                    "agent turn produced neither tools nor a review", usage=total
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
