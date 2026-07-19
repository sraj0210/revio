"""Composable fake ports used by Phase 1 tests."""

from dataclasses import dataclass, field

from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import ChangeRequest, DiffFile, ReviewRequest, ReviewResult


@dataclass
class FakeSCMReader:
    change_request: ChangeRequest
    diff_files: list[DiffFile]
    requested_targets: list[ChangeRequestTarget] = field(
        default_factory=lambda: list[ChangeRequestTarget]()
    )

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        self.requested_targets.append(target)
        return self.change_request

    async def get_diff(self, target: ChangeRequestTarget) -> list[DiffFile]:
        self.requested_targets.append(target)
        return self.diff_files


@dataclass
class FakeAIReviewer:
    result: ReviewResult
    requests: list[ReviewRequest] = field(default_factory=lambda: list[ReviewRequest]())

    async def review(self, request: ReviewRequest) -> ReviewResult:
        self.requests.append(request)
        return self.result
