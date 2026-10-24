"""Anthropic revalidation request shape, parsing, and outcome classification."""

import json
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.ai.anthropic import AnthropicReviewAdapter, anthropic_model_profile
from revio.config.anthropic import AnthropicSettings
from revio.config.review import ReviewSettings
from revio.domain.lifecycle import RevalidationOutcome
from revio.domain.revalidation import FindingRevalidationRequest, RevalidationRequestItem
from revio.errors import (
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallObservedTerminalError,
)


def _request() -> FindingRevalidationRequest:
    return FindingRevalidationRequest(
        head_sha="headsha",
        items=(
            RevalidationRequestItem(
                finding_id="finding-1",
                path="src/a.py",
                rule="sql-injection",
                symbol="f",
                evidence="query = name",
                current_file_content="def f():\n    return 1\n",
            ),
        ),
        model_profile=anthropic_model_profile(),
    )


def _adapter(http: httpx.AsyncClient) -> AnthropicReviewAdapter:
    return AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )


@pytest.mark.asyncio
async def test_revalidation_request_shape_and_parse() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = cast(dict[str, object], json.loads(request.content))
        seen.append(payload)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 50})
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "results": [
                                    {
                                        "finding_id": "finding-1",
                                        "outcome": "fixed",
                                        "current_path": "src/a.py",
                                    }
                                ]
                            }
                        ),
                    }
                ],
                "usage": {"input_tokens": 40, "output_tokens": 5},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight_revalidation(_request())
    batch = await adapter.generate_revalidation(payload)
    message = seen[1]
    output = cast(dict[str, object], message["output_config"])
    output_format = cast(dict[str, object], output["format"])
    assert output_format["type"] == "json_schema"
    # The revalidation content never carries a dedicated diff; it carries bounded current files.
    assert "findings" not in str(message.get("output_config"))
    assert len(batch.results) == 1
    assert batch.results[0].finding_id == "finding-1"
    assert batch.results[0].outcome == RevalidationOutcome.FIXED
    assert batch.usage.uncached_input_tokens == 40
    await http.aclose()


@pytest.mark.asyncio
async def test_revalidation_timeout_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        raise httpx.ReadTimeout("lost", request=request)

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight_revalidation(_request())
    with pytest.raises(ProviderCallAmbiguousError):
        await adapter.generate_revalidation(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_revalidation_refusal_is_observed_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        return httpx.Response(
            200,
            json={
                "stop_reason": "refusal",
                "content": [],
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight_revalidation(_request())
    with pytest.raises(ProviderCallObservedTerminalError):
        await adapter.generate_revalidation(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_revalidation_malformed_json_is_malformed_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "{not json"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight_revalidation(_request())
    with pytest.raises(MalformedProviderOutputError):
        await adapter.generate_revalidation(payload)
    await http.aclose()
