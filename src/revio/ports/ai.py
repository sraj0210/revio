"""Segregated AI generation ports."""

from typing import Protocol

from revio.domain.models import ReviewRequest, ReviewResult


class AIReviewGenerator(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


class AIConversationGenerator(Protocol):
    async def generate_thread_reply(self, prompt: str) -> str: ...
