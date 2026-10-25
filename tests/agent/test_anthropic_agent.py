"""Anthropic agent-turn wire format: tool calls, final submit, refusal, and tool-result append."""

from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.ai.anthropic import AnthropicReviewAdapter
from revio.config.anthropic import AnthropicSettings
from revio.config.review import ReviewSettings
from revio.domain.agent import AgentToolResult
from revio.errors import ProviderCallObservedTerminalError


def _adapter(handler: object) -> AnthropicReviewAdapter:
    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(cast(Any, handler)),
    )
    return AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )


def _payload() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "review this"}],
    }


@pytest.mark.asyncio
async def test_tool_use_turn_yields_tool_calls_and_grows_conversation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_file",
                        "input": {"path": "a.py"},
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    adapter = _adapter(handler)
    turn = await adapter.agent_turn(_payload())
    assert turn.final_review is None
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].call_id == "toolu_1"
    assert turn.tool_calls[0].name == "get_file"
    assert turn.tool_calls[0].arguments == {"path": "a.py"}
    # The assistant turn is appended so the next turn can carry tool results.
    messages = cast(list[dict[str, Any]], turn.payload["messages"])
    assert messages[-1]["role"] == "assistant"
    await adapter.close()


@pytest.mark.asyncio
async def test_submit_review_turn_is_final() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "submit_review",
                        "input": {
                            "summary": "One issue.",
                            "findings": [
                                {
                                    "category": "correctness",
                                    "title": "Bug",
                                    "explanation": "Off by one.",
                                    "confidence": 0.9,
                                    "path": "a.py",
                                    "line": 1,
                                    "identity": {
                                        "rule": "off-by-one",
                                        "symbol": "f",
                                        "evidence": "i <= n",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 4},
            },
        )

    adapter = _adapter(handler)
    turn = await adapter.agent_turn(_payload())
    assert turn.tool_calls == ()
    assert turn.final_review is not None
    assert turn.final_review.summary == "One issue."
    assert len(turn.final_review.findings) == 1
    assert turn.final_review.findings[0].identity is not None
    assert turn.final_review.findings[0].identity.rule == "off-by-one"
    await adapter.close()


@pytest.mark.asyncio
async def test_refusal_turn_is_observed_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stop_reason": "refusal",
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    adapter = _adapter(handler)
    with pytest.raises(ProviderCallObservedTerminalError):
        await adapter.agent_turn(_payload())
    await adapter.close()


@pytest.mark.asyncio
async def test_append_tool_results_builds_tool_result_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    adapter = _adapter(handler)
    results = (
        AgentToolResult(call_id="toolu_1", ok=True, content="def f(): pass"),
        AgentToolResult(call_id="toolu_9", ok=False, content="path is excluded from review"),
    )
    payload = adapter.append_tool_results(_payload(), results)
    message = cast(list[dict[str, Any]], payload["messages"])[-1]
    assert message["role"] == "user"
    blocks = cast(list[dict[str, Any]], message["content"])
    assert blocks[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "def f(): pass",
        "is_error": False,
    }
    assert blocks[1]["is_error"] is True
    await adapter.close()
