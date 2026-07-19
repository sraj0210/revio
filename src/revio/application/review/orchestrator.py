"""Minimal provider-neutral review orchestration skeleton."""

from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import ReviewRequest, ReviewResult
from revio.ports.ai import AIReviewGenerator
from revio.ports.scm import ChangeRequestReadPort


class ReviewOrchestrator:
    def __init__(self, scm: ChangeRequestReadPort, ai: AIReviewGenerator) -> None:
        self._scm = scm
        self._ai = ai

    async def review(
        self, target: ChangeRequestTarget, model_profile: ResolvedModelProfile
    ) -> ReviewResult:
        change_request = await self._scm.get_change_request(target)
        diff_files = await self._scm.get_diff(target)
        request = ReviewRequest(
            change_request=change_request,
            diff_files=tuple(diff_files),
            model_profile=model_profile,
        )
        return await self._ai.review(request)
