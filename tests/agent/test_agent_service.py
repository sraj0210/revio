"""The bounded agent loop: convergence, non-convergence, malformed, and ambiguity propagation."""

import pytest

from venaroq.application.review.agent import AgentReviewService
from venaroq.application.review.agent_tools import AgentLimits
from venaroq.domain.agent import AgentToolRequest, AgentTurnResult
from venaroq.domain.models import (
    ChangeRequest,
    ChangeRequestTarget,
    Finding,
    FindingIdentity,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
    TreeCollection,
)
from venaroq.errors import (
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallObservedTerminalError,
)


class FakeContent:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        return self._files.get(path)

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        return TreeCollection()


class FakeAgentAdapter:
    """Replays a scripted sequence of turns; raises if a scripted item is an exception."""

    def __init__(self, turns: list[AgentTurnResult | Exception]) -> None:
        self.turns = turns
        self.index = 0
        self.appended: list[tuple[object, ...]] = []

    async def agent_preflight(self, request: ReviewRequest) -> dict[str, object]:
        return {"messages": [], "_venaroq_estimated_input_tokens": 1}

    async def agent_turn(self, payload: dict[str, object]) -> AgentTurnResult:
        item = self.turns[self.index]
        self.index += 1
        if isinstance(item, Exception):
            raise item
        return item

    def append_tool_results(
        self, payload: dict[str, object], results: tuple[object, ...]
    ) -> dict[str, object]:
        self.appended.append(results)
        return payload


def _request(change_request: ChangeRequest) -> ReviewRequest:
    from venaroq.adapters.ai.anthropic import anthropic_agent_model_profile

    return ReviewRequest(
        change_request=change_request, diff_files=(), model_profile=anthropic_agent_model_profile()
    )


def _service(
    adapter: FakeAgentAdapter,
    content: FakeContent,
    *,
    max_turns: int = 4,
    limits: AgentLimits | None = None,
) -> AgentReviewService:
    return AgentReviewService(adapter, content, limits=limits or AgentLimits(), max_turns=max_turns)


def make_finding(confidence: float = 0.9) -> Finding:
    return Finding(
        category="correctness",
        title="Bug",
        explanation="Off by one.",
        confidence=confidence,
        path="a.py",
        line=1,
        identity=FindingIdentity(rule="off-by-one", symbol="f", evidence="bad()"),
    )


@pytest.mark.asyncio
async def test_converges_after_tool_use_with_provenance(change_request: ChangeRequest) -> None:
    turns: list[AgentTurnResult | Exception] = [
        AgentTurnResult(
            tool_calls=(
                AgentToolRequest(call_id="t1", name="get_file", arguments={"path": "a.py"}),
            ),
            usage=TokenUsage(output_tokens=3),
            payload={"messages": []},
        ),
        AgentTurnResult(
            final_review=ReviewResult(findings=(), summary="Looks correct."),
            usage=TokenUsage(output_tokens=5),
        ),
    ]
    adapter = FakeAgentAdapter(turns)
    service = _service(adapter, FakeContent({"a.py": "def f(): return 1"}))
    payload = await service.preflight(_request(change_request))
    result = await service.generate_preflighted(payload)
    assert result.summary == "Looks correct."
    assert result.partial is False
    assert result.usage.output_tokens == 8  # summed across turns
    assert result.provenance is not None
    assert result.provenance.consulted[0].path == "a.py"
    assert len(adapter.appended) == 1  # tool results fed back once


@pytest.mark.asyncio
async def test_non_convergence_yields_partial(change_request: ChangeRequest) -> None:
    # The model keeps asking for tools and never submits a review.
    loop_turn = AgentTurnResult(
        tool_calls=(AgentToolRequest(call_id="t", name="get_tree", arguments={"path": ""}),),
        usage=TokenUsage(output_tokens=1),
        payload={"messages": []},
    )
    adapter = FakeAgentAdapter([loop_turn] * 10)
    service = _service(adapter, FakeContent({}), max_turns=3)
    payload = await service.preflight(_request(change_request))
    result = await service.generate_preflighted(payload)
    assert result.partial is True
    assert result.findings == ()
    assert result.provenance is not None and result.provenance.complete is False


@pytest.mark.asyncio
async def test_turn_without_tools_or_review_is_malformed(change_request: ChangeRequest) -> None:
    adapter = FakeAgentAdapter([AgentTurnResult(usage=TokenUsage(output_tokens=2))])
    service = _service(adapter, FakeContent({}))
    payload = await service.preflight(_request(change_request))
    with pytest.raises(MalformedProviderOutputError):
        await service.generate_preflighted(payload)


@pytest.mark.asyncio
async def test_ambiguous_turn_propagates_and_does_not_retransmit(
    change_request: ChangeRequest,
) -> None:
    adapter = FakeAgentAdapter([ProviderCallAmbiguousError("turn lost")])
    service = _service(adapter, FakeContent({}))
    payload = await service.preflight(_request(change_request))
    with pytest.raises(ProviderCallAmbiguousError):
        await service.generate_preflighted(payload)
    assert adapter.index == 1  # the ambiguous turn was never retried


@pytest.mark.asyncio
async def test_review_quoting_fetched_content_is_withheld(change_request: ChangeRequest) -> None:
    secret = "X" * 200  # a long verbatim span the model copies out of a fetched file
    turns: list[AgentTurnResult | Exception] = [
        AgentTurnResult(
            tool_calls=(
                AgentToolRequest(call_id="t1", name="get_file", arguments={"path": "a.py"}),
            ),
            usage=TokenUsage(output_tokens=1),
            payload={"messages": []},
        ),
        AgentTurnResult(
            final_review=ReviewResult(findings=(make_finding(),), summary=secret),
            usage=TokenUsage(output_tokens=1),
        ),
    ]
    service = _service(FakeAgentAdapter(turns), FakeContent({"a.py": secret}))
    payload = await service.preflight(_request(change_request))
    result = await service.generate_preflighted(payload)
    assert result.findings == ()  # the whole review is withheld, not just scrubbed
    assert "X" * 160 not in result.summary
    assert result.partial is True


@pytest.mark.asyncio
async def test_converged_keeps_findings_under_incomplete_context(
    change_request: ChangeRequest,
) -> None:
    # Turn 1 requests a file that exceeds the per-file byte budget -> refused, context incomplete.
    turns: list[AgentTurnResult | Exception] = [
        AgentTurnResult(
            tool_calls=(
                AgentToolRequest(call_id="t1", name="get_file", arguments={"path": "big.py"}),
            ),
            usage=TokenUsage(output_tokens=1),
            payload={"messages": []},
        ),
        AgentTurnResult(
            final_review=ReviewResult(findings=(make_finding(),), summary="One issue."),
            usage=TokenUsage(output_tokens=1),
        ),
    ]
    service = _service(
        FakeAgentAdapter(turns),
        FakeContent({"big.py": "y" * 100}),
        limits=AgentLimits(max_bytes_per_file=10),
    )
    payload = await service.preflight(_request(change_request))
    result = await service.generate_preflighted(payload)
    assert result.partial is False  # a converged review stands
    assert len(result.findings) == 1  # findings are kept under limited context
    assert result.provenance is not None and result.provenance.complete is False


@pytest.mark.asyncio
async def test_usage_is_accumulated_when_a_later_turn_is_terminal(
    change_request: ChangeRequest,
) -> None:
    turns: list[AgentTurnResult | Exception] = [
        AgentTurnResult(
            tool_calls=(AgentToolRequest(call_id="t", name="get_tree", arguments={"path": ""}),),
            usage=TokenUsage(output_tokens=3),
            payload={"messages": []},
        ),
        ProviderCallObservedTerminalError(
            "stopped", usage=TokenUsage(output_tokens=5), reason="max_tokens"
        ),
    ]
    service = _service(FakeAgentAdapter(turns), FakeContent({}))
    payload = await service.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedTerminalError) as excinfo:
        await service.generate_preflighted(payload)
    assert isinstance(excinfo.value.usage, TokenUsage)
    assert excinfo.value.usage.output_tokens == 8  # 3 (turn 1) + 5 (terminal turn)
