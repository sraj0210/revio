"""The bounded agent loop: convergence, non-convergence, malformed, and ambiguity propagation."""

import pytest

from revio.application.review.agent import AgentReviewService
from revio.application.review.agent_tools import AgentLimits
from revio.domain.agent import AgentToolRequest, AgentTurnResult
from revio.domain.models import (
    ChangeRequest,
    ChangeRequestTarget,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
    TreeCollection,
)
from revio.errors import MalformedProviderOutputError, ProviderCallAmbiguousError


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
        self._turns = turns
        self._index = 0
        self.appended: list[tuple[object, ...]] = []

    async def agent_preflight(self, request: ReviewRequest) -> dict[str, object]:
        return {"messages": [], "_revio_estimated_input_tokens": 1}

    async def agent_turn(self, payload: dict[str, object]) -> AgentTurnResult:
        item = self._turns[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item

    def append_tool_results(
        self, payload: dict[str, object], results: tuple[object, ...]
    ) -> dict[str, object]:
        self.appended.append(results)
        return payload


def _request(change_request: ChangeRequest) -> ReviewRequest:
    from revio.adapters.ai.anthropic import anthropic_agent_model_profile

    return ReviewRequest(
        change_request=change_request, diff_files=(), model_profile=anthropic_agent_model_profile()
    )


def _service(
    adapter: FakeAgentAdapter, content: FakeContent, *, max_turns: int = 4
) -> AgentReviewService:
    return AgentReviewService(adapter, content, limits=AgentLimits(), max_turns=max_turns)


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
    assert adapter._index == 1  # the ambiguous turn was never retried
