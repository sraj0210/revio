"""Native Anthropic request-profile and usage contracts."""

import json
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.ai.anthropic import AnthropicReviewAdapter, anthropic_model_profile
from revio.config.anthropic import AnthropicSettings
from revio.config.review import ReviewSettings
from revio.domain.models import ChangeRequest, DiffFile, DiffLine, ReviewRequest
from revio.errors import (
    ProviderCallAmbiguousError,
    ProviderCallObservedTerminalError,
    ProviderCallSafeRetryError,
    ProviderTransientError,
)


@pytest.mark.asyncio
async def test_request_profile_structured_output_and_observed_usage(
    change_request: ChangeRequest,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = cast(object, json.loads(request.content))
        assert isinstance(payload, dict)
        seen.append(cast(dict[str, object], payload))
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "One issue found.",
                                "findings": [
                                    {
                                        "category": "correctness",
                                        "title": "Wrong branch",
                                        "explanation": "The condition is inverted.",
                                        "confidence": 0.95,
                                        "path": "a.py",
                                        "line": 1,
                                    }
                                ],
                            }
                        ),
                    }
                ],
                "usage": {
                    "input_tokens": 80,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 20,
                },
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )
    request = ReviewRequest(
        change_request=change_request,
        diff_files=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        model_profile=anthropic_model_profile(),
    )
    result = await adapter.review(request)
    message = seen[1]
    assert message["model"] == "claude-sonnet-5"
    assert message["thinking"] == {"type": "disabled"}
    assert "temperature" not in message and "top_p" not in message and "top_k" not in message
    output = cast(dict[str, object], message["output_config"])
    output_format = cast(dict[str, object], output["format"])
    assert output_format["type"] == "json_schema"
    assert "cache_control" not in message
    system = cast(list[dict[str, object]], message["system"])
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert result.usage.uncached_input_tokens == 80
    assert result.usage.cached_input_tokens == 10
    await http.aclose()


@pytest.mark.asyncio
async def test_messages_timeout_is_ambiguous(change_request: ChangeRequest) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        raise httpx.ReadTimeout("lost response", request=request)

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )
    request = ReviewRequest(
        change_request=change_request,
        diff_files=(),
        model_profile=anthropic_model_profile(),
    )
    with pytest.raises(ProviderCallAmbiguousError):
        await adapter.review(request)
    assert calls == 2
    await http.aclose()


def _request(change_request: ChangeRequest) -> ReviewRequest:
    return ReviewRequest(
        change_request=change_request,
        diff_files=(),
        model_profile=anthropic_model_profile(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "429", "529"])
async def test_token_count_availability_failures_are_safe_preflight_retries(
    change_request: ChangeRequest, outcome: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if outcome == "timeout":
            raise httpx.ReadTimeout("count timeout", request=request)
        return httpx.Response(int(outcome))

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )
    adapter = AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )
    with pytest.raises(ProviderTransientError):
        await adapter.preflight(_request(change_request))
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 529])
async def test_messages_explicit_retry_rejection_is_not_ambiguous(
    change_request: ChangeRequest, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        return httpx.Response(status)

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallSafeRetryError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_refusal_and_max_tokens_are_terminal_generation_outcomes(
    change_request: ChangeRequest, stop_reason: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 1})
        return httpx.Response(
            200,
            json={
                "stop_reason": stop_reason,
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedTerminalError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


def test_direct_anthropic_key_has_same_size_limit_as_file_key() -> None:
    with pytest.raises(ValueError, match="invalid size"):
        AnthropicReviewAdapter(
            AnthropicSettings(
                anthropic_enabled=True,
                anthropic_api_key=SecretStr("x" * 16_385),
            ),
            ReviewSettings(review_enabled=True),
        )
