"""Segregated AI generation ports."""

from typing import Protocol

from revio.domain.models import ReviewRequest, ReviewResult
from revio.domain.revalidation import FindingRevalidationBatchResult, FindingRevalidationRequest


class AIReviewGenerator(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


class AIConversationGenerator(Protocol):
    async def generate_thread_reply(self, prompt: str) -> str: ...


class AIFindingRevalidator(Protocol):
    """Bounded, structured three-state revalidation of prior findings.

    The lifecycle driver owns the durable ProviderCall reserve/attempt/response transitions;
    these methods only build the bounded request payload and classify provider outcomes by
    raising the shared provider-call error taxonomy.
    """

    async def preflight_revalidation(
        self, request: FindingRevalidationRequest
    ) -> dict[str, object]: ...
    async def generate_revalidation(
        self, payload: dict[str, object]
    ) -> FindingRevalidationBatchResult: ...
