"""Provider-neutral durable queue models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from revio.domain.events import ReviewEvent


class QueueState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    DEAD = "dead"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.DEAD, self.CANCELLED, self.SUPERSEDED}


class IngressDisposition(StrEnum):
    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    IGNORED = "ignored"
    CONFLICT = "conflict"
    CAPACITY = "capacity"


class IngressReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)
    disposition: IngressDisposition
    job_id: str | None = None


class InstallationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class InstallationState(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: str
    installation_id: str
    state: InstallationStatus
    provider_updated_at: datetime | None = None
    ordering_delivery_identity: str
    updated_at: datetime


class QueueJob(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    provider_id: str
    job_type: str
    semantic_identity: str
    event_schema_version: int = 1
    event: ReviewEvent
    state: QueueState
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    terminal_at: datetime | None = None


class JobLease(BaseModel):
    model_config = ConfigDict(frozen=True)
    job: QueueJob
    worker_id: str
    attempt_number: int = Field(gt=0)
    expires_at: datetime
