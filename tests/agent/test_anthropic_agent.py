"""Anthropic agent-turn wire format: tool calls, final submit, refusal, and tool-result append."""

from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from venaroq.adapters.ai.anthropic import AnthropicReviewAdapter
from venaroq.adapters.ai.anthropic.adapter import (
    _AGENT_TOOLS,  # pyright: ignore[reportPrivateUsage]
)
from venaroq.config.anthropic import AnthropicSettings
from venaroq.config.review import ReviewSettings
from venaroq.domain.agent import AgentToolResult
from venaroq.errors import MalformedProviderOutputError, ProviderCallObservedTerminalError


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
async def test_real_multi_turn_round_trip_carries_conversation() -> None:
    # Turn 1 asks for a file; after append_tool_results, turn 2 submits the review. Assert the grown
    # conversation (assistant tool_use + user tool_result) is carried into the second request.
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = cast(dict[str, Any], _json.loads(request.content))
        calls.append(body)
        if len(calls) == 1:
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
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "submit_review",
                        "input": {"summary": "ok", "findings": []},
                    }
                ],
                "usage": {"input_tokens": 9, "output_tokens": 3},
            },
        )

    adapter = _adapter(handler)
    turn1 = await adapter.agent_turn(_payload())
    assert turn1.tool_calls[0].call_id == "toolu_1"
    results = (AgentToolResult(call_id="toolu_1", ok=True, content="def f(): pass"),)
    next_payload = adapter.append_tool_results(turn1.payload, results)
    turn2 = await adapter.agent_turn(next_payload)
    assert turn2.final_review is not None and turn2.final_review.summary == "ok"
    # The second request's messages must include the assistant tool_use and the user tool_result.
    second_messages = cast(list[dict[str, Any]], calls[1]["messages"])
    roles = [m["role"] for m in second_messages]
    assert roles == ["user", "assistant", "user"]
    tool_result_block = cast(list[dict[str, Any]], second_messages[2]["content"])[0]
    assert tool_result_block["tool_use_id"] == "toolu_1"
    assert tool_result_block["content"] == "def f(): pass"
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


def _strict_compatible(schema: dict[str, Any], where: str) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, where
        assert set(schema.get("required", [])) == set(schema.get("properties", {})), where
        for name, child in schema.get("properties", {}).items():
            _strict_compatible(child, f"{where}.{name}")
    if schema.get("type") == "array":
        _strict_compatible(schema["items"], f"{where}[]")
    for unsupported in ("minLength", "maxLength", "minimum", "maximum", "multipleOf"):
        assert unsupported not in schema, f"{where}: {unsupported} is not allowed in strict mode"


def test_agent_tools_are_strict_with_compatible_schemas() -> None:
    # Strict tool use is what guarantees schema-valid submit_review input (live evals showed
    # ~4.5% malformed agent turns without it).
    for tool in _AGENT_TOOLS:
        assert tool["strict"] is True, tool["name"]
        _strict_compatible(tool["input_schema"], tool["name"])


def _respond(content: list[dict[str, Any]]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": content,
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (
            # The live failure: `findings` folded into the summary string, so it is missing.
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "submit_review",
                    "input": {"summary": 'ok</summary>\n<parameter name="findings">[]'},
                }
            ],
            "submit_review:schema_invalid:findings:missing",
        ),
        (
            [{"type": "text", "text": "I reviewed it and found nothing."}],
            "text_reply:schema_invalid:root:json_invalid",
        ),
        ([], "text_reply:no_tool_call_or_text"),
    ],
)
async def test_malformed_agent_turn_names_a_content_free_detail(
    content: list[dict[str, Any]], detail: str
) -> None:
    adapter = _adapter(_respond(content))
    with pytest.raises(MalformedProviderOutputError) as raised:
        await adapter.agent_turn(_payload())
    assert raised.value.detail == detail
    # The detail never echoes model output.
    assert "found nothing" not in raised.value.detail
    assert "parameter" not in raised.value.detail
    await adapter.close()
