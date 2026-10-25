"""Bounded native OpenAI Responses API review generation.

This adapter is interchangeable with the Anthropic adapter: it reuses the shared ``review-v2``
structured contract (DTOs, JSON schema, system prompts, finding builders) from
``revio.adapters.ai.review_schema`` and maps OpenAI outcomes onto the same provider-call error
taxonomy and ``TokenUsage`` accounting. Only the transport, structured-output dialect, usage
parsing, and local token estimation are OpenAI-specific. OpenAI has no bounded token-count endpoint,
so preflight estimates input tokens locally (the profile declares ``token_counting=False``).
"""

import json
from typing import Any, cast

import httpx
from pydantic import ValidationError

from revio.adapters.ai.review_schema import (
    REVALIDATION_JSON_SCHEMA,
    REVALIDATION_SYSTEM_PROMPT,
    REVIEW_JSON_SCHEMA,
    REVIEW_SYSTEM_PROMPT,
    RevalidationBatchDTO,
    ReviewDTO,
    build_revalidation_result,
    build_review_result,
    serialize_revalidation_content,
    serialize_review_content,
)
from revio.config.openai import OpenAISettings
from revio.config.review import ReviewSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ModelAlias, ProviderId
from revio.domain.models import ReviewRequest, ReviewResult, TokenUsage
from revio.domain.revalidation import (
    FindingRevalidationBatchResult,
    FindingRevalidationRequest,
)
from revio.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallObservedInvalidResponseError,
    ProviderCallObservedTerminalError,
    ProviderCallRejectedError,
    ProviderCallSafeRetryError,
    ProviderCallTerminalError,
)

OPENAI_PROVIDER_ID = ProviderId(value="openai")

# Approximate bytes-per-token for the local input estimate. OpenAI exposes no bounded token-count
# endpoint, so preflight cannot ask the provider; a conservative ~4 bytes/token heuristic bounds
# admission without a network round-trip. Real usage is always read from the response afterward.
_ESTIMATE_BYTES_PER_TOKEN = 4


def openai_model_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        alias=ModelAlias(value="openai-review-default"),
        provider_id=OPENAI_PROVIDER_ID,
        provider_model_id="gpt-5.1",
        context_tokens=400_000,
        max_output_tokens=128_000,
        structured_output="json_schema",
        prompt_caching=True,
        allowed_review_modes=frozenset({"diff_only"}),
        profile_version="1",
        thinking="disabled",
        use_default_sampling=True,
        token_counting=False,
    )


def _load_key(settings: OpenAISettings) -> str:
    if settings.openai_api_key is not None:
        value = settings.openai_api_key.get_secret_value()
        if not value or len(value.encode()) > 16_384:
            raise ValueError("OpenAI API key has an invalid size")
        return value
    path = settings.openai_api_key_file
    if path is None:
        raise ValueError("OpenAI API key is unavailable")
    try:
        if path.stat().st_size > 16_384:
            raise ValueError("OpenAI API-key file is too large")
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ValueError("OpenAI API-key file is unavailable") from None
    if not value:
        raise ValueError("OpenAI API-key file is empty")
    return value


class OpenAIReviewAdapter:
    def __init__(
        self,
        settings: OpenAISettings,
        review_settings: ReviewSettings,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.openai_enabled:
            raise ValueError("OpenAI adapter is not enabled")
        self._review_settings = review_settings
        self._owns_http = http is None
        timeout = settings.openai_http_timeout_seconds
        self._http = http or httpx.AsyncClient(
            base_url=settings.openai_api_url,
            headers={
                "authorization": f"Bearer {_load_key(settings)}",
                "content-type": "application/json",
                "user-agent": "revio-phase8",
            },
            timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _base_payload(
        self, profile: ResolvedModelProfile, instructions: str, content: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if profile.thinking != "disabled" or not profile.use_default_sampling:
            raise ValueError("model profile is not deterministic")
        return {
            "model": profile.provider_model_id,
            "max_output_tokens": min(
                profile.max_output_tokens, self._review_settings.review_output_token_ceiling
            ),
            "reasoning": {"effort": "minimal"},
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "revio_review",
                    "schema": schema,
                    "strict": True,
                }
            },
            "store": False,
        }

    @staticmethod
    def _estimate_input_tokens(payload: dict[str, Any]) -> int:
        text = str(payload.get("instructions", ""))
        for message in cast(list[dict[str, Any]], payload.get("input", [])):
            text += str(message.get("content", ""))
        return (len(text.encode("utf-8")) // _ESTIMATE_BYTES_PER_TOKEN) + 1

    def _preflighted(self, payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
        estimated = self._estimate_input_tokens(payload)
        if estimated > self._review_settings.admission_token_ceiling:
            raise IncompleteReviewInputError(f"{reason} exceeds admission ceiling")
        return {**payload, "_revio_estimated_input_tokens": estimated}

    async def _post_responses(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._http.post("/v1/responses", json=payload, follow_redirects=False)
        except httpx.ConnectError as error:
            raise ProviderCallSafeRetryError(
                "OpenAI connection failed before acceptance"
            ) from error
        except (
            httpx.TimeoutException,
            httpx.WriteError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ):
            raise ProviderCallAmbiguousError("OpenAI Responses outcome is ambiguous") from None
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            try:
                retry_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_seconds = None
            raise ProviderCallSafeRetryError(
                "OpenAI explicitly rejected Responses for retry",
                retry_after_seconds=retry_seconds,
            )
        if response.status_code >= 500:
            raise ProviderCallAmbiguousError("OpenAI Responses outcome is ambiguous")
        if response.status_code >= 400:
            raise ProviderCallTerminalError("OpenAI Responses request was rejected")
        return response

    @staticmethod
    def _usage(body: dict[str, Any]) -> TokenUsage:
        raw_usage = body["usage"]
        if not isinstance(raw_usage, dict):
            raise TypeError
        raw_usage = cast(dict[str, object], raw_usage)
        details = raw_usage.get("input_tokens_details")
        cached: object = 0
        if isinstance(details, dict):
            cached = cast(dict[str, object], details).get("cached_tokens", 0)
        input_tokens = raw_usage["input_tokens"]
        output_tokens = raw_usage["output_tokens"]
        for value in (input_tokens, output_tokens, cached):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError
        uncached = cast(int, input_tokens) - cast(int, cached)
        if uncached < 0:
            raise ValueError
        return TokenUsage(
            uncached_input_tokens=uncached,
            cached_input_tokens=cast(int, cached),
            cache_creation_tokens=0,
            output_tokens=cast(int, output_tokens),
        )

    @staticmethod
    def _output_text(body: dict[str, Any], usage: TokenUsage) -> str:
        status = body.get("status")
        if status == "incomplete":
            details = body.get("incomplete_details")
            reason: object = "incomplete"
            if isinstance(details, dict):
                reason = cast(dict[str, object], details).get("reason", "incomplete")
            raise ProviderCallObservedTerminalError(
                f"OpenAI stopped with {reason}", usage=usage, reason=str(reason)
            )
        texts: list[str] = []
        for item in cast(list[dict[str, Any]], body["output"]):
            if item.get("type") != "message":
                continue
            for block in cast(list[dict[str, Any]], item.get("content", [])):
                block_type = block.get("type")
                if block_type == "refusal":
                    raise ProviderCallObservedTerminalError(
                        "OpenAI refused the request", usage=usage, reason="refusal"
                    )
                if block_type == "output_text":
                    texts.append(block["text"])
        if len(texts) != 1:
            raise ValueError
        return texts[0]

    async def preflight(self, request: ReviewRequest) -> dict[str, Any]:
        payload = self._base_payload(
            request.model_profile,
            REVIEW_SYSTEM_PROMPT,
            serialize_review_content(request),
            REVIEW_JSON_SCHEMA,
        )
        return self._preflighted(payload, reason="review input")

    async def generate_preflighted(self, payload: dict[str, Any]) -> ReviewResult:
        request_payload = {
            key: value for key, value in payload.items() if key != "_revio_estimated_input_tokens"
        }
        response = await self._post_responses(request_payload)
        try:
            body = cast(dict[str, Any], response.json())
            usage = self._usage(body)
            text = self._output_text(body, usage)
            parsed = ReviewDTO.model_validate_json(text)
        except (
            ProviderCallRejectedError,
            ProviderCallTerminalError,
            ProviderCallObservedTerminalError,
            ProviderCallObservedInvalidResponseError,
        ):
            raise
        except (ValidationError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            observed_usage = locals().get("usage")
            if not isinstance(observed_usage, TokenUsage):
                raise ProviderCallObservedInvalidResponseError(
                    "OpenAI 2xx Responses output could not be normalized"
                ) from None
            raise MalformedProviderOutputError(
                "OpenAI response failed local validation", usage=observed_usage
            ) from None
        return build_review_result(parsed, usage)

    async def review(self, request: ReviewRequest) -> ReviewResult:
        return await self.generate_preflighted(await self.preflight(request))

    async def repair_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        repaired = dict(payload)
        repaired.pop("_revio_estimated_input_tokens", None)
        messages = list(cast(list[dict[str, Any]], payload["input"]))
        messages.append(
            {
                "role": "user",
                "content": (
                    "Validation reason=structured_output_invalid; prompt_version=review-v2; "
                    "schema_version=review-v2. Generate the review again and strictly satisfy "
                    "the same JSON schema."
                ),
            }
        )
        repaired["input"] = messages
        return self._preflighted(repaired, reason="repair")

    async def preflight_revalidation(self, request: FindingRevalidationRequest) -> dict[str, Any]:
        payload = self._base_payload(
            request.model_profile,
            REVALIDATION_SYSTEM_PROMPT,
            serialize_revalidation_content(request),
            REVALIDATION_JSON_SCHEMA,
        )
        return self._preflighted(payload, reason="revalidation input")

    async def generate_revalidation(
        self, payload: dict[str, Any]
    ) -> FindingRevalidationBatchResult:
        request_payload = {
            key: value for key, value in payload.items() if key != "_revio_estimated_input_tokens"
        }
        response = await self._post_responses(request_payload)
        try:
            body = cast(dict[str, Any], response.json())
            usage = self._usage(body)
            text = self._output_text(body, usage)
            parsed = RevalidationBatchDTO.model_validate_json(text)
        except (
            ProviderCallRejectedError,
            ProviderCallTerminalError,
            ProviderCallObservedTerminalError,
            ProviderCallObservedInvalidResponseError,
        ):
            raise
        except (ValidationError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            observed_usage = locals().get("usage")
            if not isinstance(observed_usage, TokenUsage):
                raise ProviderCallObservedInvalidResponseError(
                    "OpenAI 2xx revalidation output could not be normalized"
                ) from None
            raise MalformedProviderOutputError(
                "OpenAI revalidation response failed local validation", usage=observed_usage
            ) from None
        return build_revalidation_result(parsed, usage)
