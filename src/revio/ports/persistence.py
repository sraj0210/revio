"""Provider-neutral durable persistence ports."""

from datetime import datetime
from typing import Literal, Protocol

from revio.domain.events import WebhookNormalizationResult
from revio.domain.queue import (
    IngressReceipt,
    InstallationState,
    JobLease,
    QueueJob,
    RetentionResult,
)
from revio.domain.reviews import (
    NormalizedReviewArtifact,
    ProviderCallIdentity,
    ProviderCallRecord,
    ProviderCallState,
    ReviewRunRecord,
    ReviewRunState,
    UsageDisposition,
    WriteOperationRecord,
    WriteOperationState,
)


class DurableIngressPort(Protocol):
    async def persist(
        self,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
    ) -> IngressReceipt: ...


class QueueRepository(Protocol):
    async def lease_next(self, worker_id: str, now: datetime) -> JobLease | None: ...
    async def heartbeat(self, lease: JobLease, now: datetime) -> bool: ...
    async def complete(
        self, lease: JobLease, *, head_sha: str, base_sha: str, now: datetime
    ) -> bool: ...
    async def terminate(self, lease: JobLease, state: str, reason: str, now: datetime) -> bool: ...
    async def retry(
        self,
        lease: JobLease,
        *,
        available_at: datetime,
        error_class: str,
        error_message: str,
        now: datetime,
    ) -> bool: ...
    async def installation_state(
        self, provider_id: str, installation_id: str
    ) -> InstallationState | None: ...
    async def get_job(self, job_id: str) -> QueueJob | None: ...
    async def recover_expired(self, now: datetime) -> int: ...


class PersistenceReadinessPort(Protocol):
    async def check_ready(self) -> bool: ...


class TerminalRetentionPort(Protocol):
    async def retain_terminal_history(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        dry_run: bool,
        correlation_id: str,
    ) -> RetentionResult: ...


class ReviewRepository(Protocol):
    async def create_run(
        self,
        *,
        run_id: str,
        job_id: str,
        head_sha: str,
        base_sha: str,
        external_id: str,
        now: datetime,
    ) -> ReviewRunRecord: ...
    async def get_run(self, run_id: str) -> ReviewRunRecord | None: ...
    async def transition_run(
        self,
        run_id: str,
        expected: ReviewRunState,
        target: ReviewRunState,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> bool: ...
    async def reserve_call(
        self, identity: ProviderCallIdentity, now: datetime
    ) -> ProviderCallRecord: ...
    async def get_call(self, call_id: str) -> ProviderCallRecord | None: ...
    async def latest_call(
        self, run_id: str, call_kind: Literal["initial", "repair"]
    ) -> ProviderCallRecord | None: ...
    async def transition_call(
        self,
        call_id: str,
        expected: ProviderCallState,
        target: ProviderCallState,
        now: datetime,
        *,
        usage: UsageDisposition | None = None,
    ) -> bool: ...
    async def persist_artifact(
        self, artifact: NormalizedReviewArtifact, expected_run_state: ReviewRunState
    ) -> bool: ...
    async def get_artifact(self, run_id: str) -> NormalizedReviewArtifact | None: ...
    async def replace_artifact(
        self, artifact: NormalizedReviewArtifact, *, expected_digest: str
    ) -> bool: ...
    async def reserve_write_operation(
        self,
        kind: Literal["check_run", "publish"],
        *,
        operation_id: str,
        run_id: str,
        head_sha: str,
        now: datetime,
        external_id: str | None = None,
        operation_key: str | None = None,
        marker: str | None = None,
        marker_key_id: str | None = None,
    ) -> WriteOperationRecord: ...
    async def transition_write_operation(
        self,
        kind: Literal["check_run", "publish"],
        operation_id: str,
        expected: WriteOperationState,
        target: WriteOperationState,
        now: datetime,
        *,
        provider_id: str | None = None,
        terminal_reason: str | None = None,
        increment_attempt: bool = False,
    ) -> bool: ...
    async def unresolved_marker_key_ids(self) -> set[str]: ...
