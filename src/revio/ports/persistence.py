"""Provider-neutral durable persistence ports."""

from datetime import datetime
from typing import Protocol

from revio.domain.events import WebhookNormalizationResult
from revio.domain.queue import (
    IngressReceipt,
    InstallationState,
    JobLease,
    QueueJob,
    RetentionResult,
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
