"""Native OpenAI Responses request shape, usage, context, and error-taxonomy contracts."""

import json
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.ai.openai import OpenAIReviewAdapter, openai_model_profile
from revio.config.openai import OpenAISettings
from revio.config.review import ReviewSettings
from revio.domain.models import ChangeRequest, DiffFile, DiffLine, ReviewRequest
from revio.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallObservedInvalidResponseError,
    ProviderCallObservedTerminalError,
    ProviderCallSafeRetryError,
    ProviderCallTerminalError,
)


def _adapter(
    http: httpx.AsyncClient, *, review_settings: ReviewSettings | None = None
) -> OpenAIReviewAdapter:
    return OpenAIReviewAdapter(
        OpenAISettings(openai_enabled=True, openai_api_key=SecretStr("secret")),
        review_settings or ReviewSettings(review_enabled=True),
        http=http,
    )


def _request(change_request: ChangeRequest) -> ReviewRequest:
    return ReviewRequest(
        change_request=change_request,
        diff_files=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        model_profile=openai_model_profile(),
    )


def _success_body(review_text: str) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": review_text}],
            }
        ],
        "usage": {
            "input_tokens": 80,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens": 20,
        },
    }


@pytest.mark.asyncio
async def test_request_profile_structured_output_and_observed_usage(
    change_request: ChangeRequest,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = cast(object, json.loads(request.content))
        assert isinstance(payload, dict)
        seen.append(cast(dict[str, object], payload))
        review_text = json.dumps(
            {
                "summary": "One issue found.",
                "findings": [
                    {
                        "category": "security",
                        "title": "Injection",
                        "explanation": "Unsanitized input.",
                        "confidence": 0.9,
                        "path": "a.py",
                        "line": 1,
                        "identity": {
                            "rule": "sql-injection",
                            "symbol": "handler",
                            "evidence": "bad()",
                        },
                    }
                ],
            }
        )
        return httpx.Response(200, json=_success_body(review_text))

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    result = await adapter.review(_request(change_request))
    message = seen[0]
    assert message["model"] == "gpt-5.1"
    assert message["store"] is False
    assert "temperature" not in message and "top_p" not in message
    text_format = cast(dict[str, object], message["text"])
    output_format = cast(dict[str, object], text_format["format"])
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    # Total input minus cached is the uncached accounting; cache creation is never billed.
    assert result.usage.uncached_input_tokens == 70
    assert result.usage.cached_input_tokens == 10
    assert result.usage.cache_creation_tokens == 0
    assert result.usage.output_tokens == 20
    assert len(result.findings) == 1
    assert result.findings[0].identity is not None
    assert result.findings[0].identity.rule == "sql-injection"
    await http.aclose()


@pytest.mark.asyncio
async def test_timeout_is_ambiguous(change_request: ChangeRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost response", request=request)

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    with pytest.raises(ProviderCallAmbiguousError):
        await adapter.review(_request(change_request))
    await http.aclose()


@pytest.mark.asyncio
async def test_connect_error_is_safe_retry(change_request: ChangeRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallSafeRetryError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, ProviderCallSafeRetryError),
        (500, ProviderCallAmbiguousError),
        (503, ProviderCallAmbiguousError),
        (400, ProviderCallTerminalError),
        (401, ProviderCallTerminalError),
        (403, ProviderCallTerminalError),
    ],
)
async def test_http_status_maps_to_taxonomy(
    change_request: ChangeRequest, status: int, expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(expected):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_refusal_is_terminal_generation_outcome(change_request: ChangeRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "no"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedTerminalError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_incomplete_max_output_tokens_is_terminal(change_request: ChangeRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedTerminalError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_2xx_without_usage_is_observed_invalid(change_request: ChangeRequest) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "completed", "output": []})

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedInvalidResponseError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_2xx_with_usage_but_malformed_json_is_malformed(
    change_request: ChangeRequest,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_body("{not valid json"))

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(MalformedProviderOutputError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_cached_tokens_exceeding_input_is_observed_invalid(
    change_request: ChangeRequest,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "{}"}],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 1,
                },
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(http)
    payload = await adapter.preflight(_request(change_request))
    with pytest.raises(ProviderCallObservedInvalidResponseError):
        await adapter.generate_preflighted(payload)
    await http.aclose()


@pytest.mark.asyncio
async def test_local_estimate_enforces_admission_ceiling(change_request: ChangeRequest) -> None:
    # OpenAI has no count endpoint; preflight must bound admission from the local estimate alone.
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("preflight must not call the provider when over the ceiling")

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    adapter = _adapter(
        http,
        review_settings=ReviewSettings(
            review_enabled=True,
            review_input_token_ceiling=10,
            review_input_token_safety_margin=9,
        ),
    )
    big_lines = tuple(DiffLine(content="x" * 100, side="new", new_line=i + 1) for i in range(50))
    request = ReviewRequest(
        change_request=change_request,
        diff_files=(DiffFile(new_path="a.py", status="added", lines=big_lines),),
        model_profile=openai_model_profile(),
    )
    with pytest.raises(IncompleteReviewInputError):
        await adapter.preflight(request)
    await http.aclose()


def test_direct_openai_key_has_size_limit() -> None:
    with pytest.raises(ValueError, match="invalid size"):
        OpenAIReviewAdapter(
            OpenAISettings(openai_enabled=True, openai_api_key=SecretStr("x" * 16_385)),
            ReviewSettings(review_enabled=True),
        )


def test_endpoint_is_locked() -> None:
    with pytest.raises(ValueError, match="supports only"):
        OpenAISettings(
            openai_enabled=True,
            openai_api_key=SecretStr("secret"),
            openai_api_url="https://azure.example.com",
        )


def test_production_requires_file_source() -> None:
    with pytest.raises(ValueError, match="production requires an OpenAI API-key file source"):
        OpenAISettings(
            environment="production",
            openai_enabled=True,
            openai_api_key=SecretStr("secret"),
        )
