"""Segregated source-control ports."""

from typing import Protocol, runtime_checkable

from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.models import ChangeRequest, DiffCollection, Finding, TreeCollection
from revio.domain.reviews import ReconciliationResult, ReviewStatusDetails


@runtime_checkable
class ChangeRequestReadPort(Protocol):
    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest: ...
    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection: ...


@runtime_checkable
class RepositoryContentReadPort(Protocol):
    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None: ...
    async def get_tree(
        self, target: ChangeRequestTarget, path: str, ref: str
    ) -> TreeCollection: ...


class ReviewPublisherPort(Protocol):
    async def publish_review(
        self, target: ChangeRequestTarget, summary: str, findings: list[Finding], operation_key: str
    ) -> str: ...


class ReviewStatusPort(Protocol):
    async def set_review_status(self, target: ChangeRequestTarget, status: str) -> None: ...


class ThreadReaderPort(Protocol):
    async def list_review_threads(self, target: ChangeRequestTarget) -> list[str]: ...


class ThreadResolverPort(Protocol):
    async def resolve_thread(self, target: ChangeRequestTarget, thread_id: str) -> None: ...


class ThreadReplyPort(Protocol):
    async def reply_to_thread(
        self, target: ChangeRequestTarget, thread_id: str, body: str, operation_key: str
    ) -> str: ...


class ReviewWriterPort(Protocol):
    async def reconcile_check_run(
        self,
        target: ChangeRequestTarget,
        *,
        head_sha: str,
        external_id: str,
        name: str = "Revio review",
    ) -> ReconciliationResult: ...
    async def create_check_run(
        self, target: ChangeRequestTarget, details: ReviewStatusDetails
    ) -> str: ...
    async def update_check_run(
        self,
        target: ChangeRequestTarget,
        provider_id: str,
        details: ReviewStatusDetails,
    ) -> None: ...
    async def get_check_run(
        self, target: ChangeRequestTarget, provider_id: str
    ) -> dict[str, object]: ...
    async def reconcile_review(
        self, target: ChangeRequestTarget, *, marker: str
    ) -> ReconciliationResult: ...
    def validate_inline_findings(
        self, findings: tuple[Finding, ...], diff: DiffCollection
    ) -> tuple[Finding, ...]: ...
    async def publish_review(
        self,
        target: ChangeRequestTarget,
        *,
        summary: str,
        findings: tuple[Finding, ...],
        marker: str,
        commit_id: str,
    ) -> str: ...
