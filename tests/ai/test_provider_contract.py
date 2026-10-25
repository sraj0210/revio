"""Cross-provider review contract.

The same structured review output must map to an identical domain ``ReviewResult`` regardless of
which provider produced it. Each harness wires a provider-native mock transport that embeds a given
``review-v2`` JSON document; the assertions below are provider-agnostic and run against both.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import pytest
from pydantic import SecretStr

from venaroq.adapters.ai.anthropic import AnthropicReviewAdapter, anthropic_model_profile
from venaroq.adapters.ai.openai import OpenAIReviewAdapter, openai_model_profile
from venaroq.config.anthropic import AnthropicSettings
from venaroq.config.openai import OpenAISettings
from venaroq.config.review import ReviewSettings
from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.models import ChangeRequest, DiffFile, DiffLine, ReviewRequest, ReviewResult


class _ReviewAdapter(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class ProviderHarness:
    name: str
    profile: ResolvedModelProfile
    make_adapter: Callable[[str], _ReviewAdapter]


def _anthropic_adapter(review_text: str) -> _ReviewAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": review_text}],
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
    return AnthropicReviewAdapter(
        AnthropicSettings(anthropic_enabled=True, anthropic_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )


def _openai_adapter(review_text: str) -> _ReviewAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": review_text}],
                    },
                ],
                "usage": {
                    "input_tokens": 80,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens": 20,
                },
            },
        )

    http = httpx.AsyncClient(
        base_url="https://api.openai.com", transport=httpx.MockTransport(handler)
    )
    return OpenAIReviewAdapter(
        OpenAISettings(openai_enabled=True, openai_api_key=SecretStr("secret")),
        ReviewSettings(review_enabled=True),
        http=http,
    )


HARNESSES = [
    ProviderHarness("anthropic", anthropic_model_profile(), _anthropic_adapter),
    ProviderHarness("openai", openai_model_profile(), _openai_adapter),
]


def _request(change_request: ChangeRequest, profile: ResolvedModelProfile) -> ReviewRequest:
    return ReviewRequest(
        change_request=change_request,
        diff_files=(
            DiffFile(
                new_path="a.py",
                status="added",
                lines=(DiffLine(content="bad()", side="new", new_line=1),),
            ),
        ),
        model_profile=profile,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
async def test_identical_structured_output_maps_identically(
    change_request: ChangeRequest, harness: ProviderHarness
) -> None:
    review_text = json.dumps(
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
                    "identity": {
                        "rule": "Inverted-Condition",
                        "symbol": "handler",
                        "evidence": "bad()",
                    },
                }
            ],
        }
    )
    adapter = harness.make_adapter(review_text)
    result = await adapter.review(_request(change_request, harness.profile))
    await adapter.close()

    assert result.summary == "One issue found."
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.category == "correctness"
    assert finding.title == "Wrong branch"
    assert finding.confidence == 0.95
    assert finding.path == "a.py"
    assert finding.line == 1
    assert finding.identity is not None
    # The shared builder normalizes the rule to lower-case for every provider.
    assert finding.identity.rule == "inverted-condition"
    assert finding.identity.evidence == "bad()"
    assert result.usage.output_tokens == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
async def test_empty_findings_map_to_no_findings(
    change_request: ChangeRequest, harness: ProviderHarness
) -> None:
    review_text = json.dumps({"summary": "No issues.", "findings": []})
    adapter = harness.make_adapter(review_text)
    result = await adapter.review(_request(change_request, harness.profile))
    await adapter.close()
    assert result.findings == ()
    assert result.summary == "No issues."


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", HARNESSES, ids=lambda h: h.name)
async def test_out_of_bounds_identity_is_dropped_not_fatal(
    change_request: ChangeRequest, harness: ProviderHarness
) -> None:
    # Evidence beyond the strict domain bound is tolerated by the lenient DTO but produces a
    # finding with identity=None after strict core re-validation, for every provider.
    review_text = json.dumps(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "category": "correctness",
                    "title": "Wrong branch",
                    "explanation": "The condition is inverted.",
                    "confidence": 0.5,
                    "path": "a.py",
                    "line": 1,
                    "identity": {
                        "rule": "inverted-condition",
                        "symbol": None,
                        "evidence": "x" * 4000,
                    },
                }
            ],
        }
    )
    adapter = harness.make_adapter(review_text)
    result = await adapter.review(_request(change_request, harness.profile))
    await adapter.close()
    assert len(result.findings) == 1
    assert result.findings[0].identity is None
