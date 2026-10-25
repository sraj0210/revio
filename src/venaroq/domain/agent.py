"""Provider-neutral value objects for agent-mode (bounded tool-assisted) review.

Agent mode lets a capable model verify diff findings against bounded repository context through two
read-only tools (``get_file``/``get_tree``). These objects are the neutral contract between the core
agent loop, the guarded tool executor, and the provider adapters. No provider- or SCM-specific type
appears here. The verification-provenance value objects (``ConsultedFile``, ``AgentProvenance``)
live in ``venaroq.domain.models`` so ``ReviewResult`` can carry them without a circular import.
"""

from pydantic import BaseModel, ConfigDict, Field

from venaroq.domain.models import ReviewResult, TokenUsage


class AgentToolRequest(BaseModel):
    """A tool call the model asked for, carried verbatim from the provider turn."""

    model_config = ConfigDict(frozen=True)
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=64)
    # Raw, unvalidated arguments as the model produced them; the core validates before any fetch.
    arguments: dict[str, object] = Field(default_factory=dict)


class AgentToolResult(BaseModel):
    """The core's bounded, guarded response to one tool call."""

    model_config = ConfigDict(frozen=True)
    call_id: str = Field(min_length=1, max_length=256)
    ok: bool
    # Fetched content on success, or a short, non-reproducing refusal reason on failure. Never
    # contains secrets; refusals are phrased structurally (e.g. "path is excluded").
    content: str = Field(default="", max_length=200_000)


class AgentTurnResult(BaseModel):
    """The outcome of one model turn in the agent loop.

    Exactly one of ``final_review`` or ``tool_calls`` is populated. ``payload`` is the opaque,
    adapter-owned conversation state to continue from; the core appends tool results to it via the
    adapter before the next turn.
    """

    model_config = ConfigDict(frozen=True)
    final_review: ReviewResult | None = None
    tool_calls: tuple[AgentToolRequest, ...] = ()
    usage: TokenUsage = TokenUsage()
    payload: dict[str, object] = Field(default_factory=dict)
