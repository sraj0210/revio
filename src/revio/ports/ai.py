"""Segregated AI generation ports."""

from typing import Protocol

from revio.domain.agent import AgentToolResult, AgentTurnResult
from revio.domain.models import ReviewRequest, ReviewResult
from revio.domain.revalidation import FindingRevalidationBatchResult, FindingRevalidationRequest


class AIReviewGenerator(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


class AgentReviewGenerator(Protocol):
    """A model turn primitive for bounded, tool-assisted review.

    The provider-neutral core owns the loop, the limits, and tool execution; the adapter only
    translates one turn into either tool requests or a final structured review, and appends the
    core's tool results to its own opaque conversation state. The adapter never touches the SCM.
    """

    async def agent_preflight(self, request: ReviewRequest) -> dict[str, object]:
        """Build the initial agent conversation payload (messages + tool definitions)."""
        ...

    async def agent_turn(self, payload: dict[str, object]) -> AgentTurnResult: ...

    def append_tool_results(
        self, payload: dict[str, object], results: tuple[AgentToolResult, ...]
    ) -> dict[str, object]:
        """Append the core's tool results to the conversation and return the next payload."""
        ...


class AIConversationGenerator(Protocol):
    async def generate_thread_reply(self, prompt: str) -> str: ...


class AIFindingRevalidator(Protocol):
    """Bounded, structured three-state revalidation of prior findings.

    The lifecycle driver owns the durable ProviderCall reserve/attempt/response transitions;
    these methods only build the bounded request payload and classify provider outcomes by
    raising the shared provider-call error taxonomy.
    """

    async def preflight_revalidation(
        self, request: FindingRevalidationRequest
    ) -> dict[str, object]: ...
    async def generate_revalidation(
        self, payload: dict[str, object]
    ) -> FindingRevalidationBatchResult: ...
