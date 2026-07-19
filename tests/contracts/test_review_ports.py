"""Reusable behavioral contract for review-generation ports."""

import pytest

from revio.application.review.orchestrator import ReviewOrchestrator
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import ChangeRequest, DiffFile, DiffLine, ReviewResult
from tests.fakes import FakeAIReviewer, FakeSCMReader


@pytest.mark.asyncio
async def test_fake_ports_drive_provider_neutral_review(
    target: ChangeRequestTarget, change_request: ChangeRequest, model_profile: ResolvedModelProfile
) -> None:
    diff = DiffFile(
        new_path="src/example.py",
        status="modified",
        lines=(DiffLine(content="x = 1", side="new", new_line=1),),
    )
    scm = FakeSCMReader(change_request, [diff])
    ai = FakeAIReviewer(ReviewResult(summary="Reviewed one file"))
    result = await ReviewOrchestrator(scm, ai).review(target, model_profile)
    assert result.summary == "Reviewed one file"
    assert ai.requests[0].diff_files == (diff,)
    assert scm.requested_targets == [target, target]
