"""Durable provider-neutral Phase 6 finding-lifecycle models.

These records never store full current files, raw provider bodies, prompts, credentials, or
generated prose as fingerprint identity. The only source-derived value retained is the bounded
normalized evidence snippet needed for revalidation, capped at the same 2 KiB ceiling as the
generated finding identity.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from venaroq.domain.models import TokenUsage
from venaroq.domain.reviews import ProviderCallState, WriteOperationState

# Resolution operations reuse the Phase 4 write-operation state machine verbatim.
ThreadResolutionState = WriteOperationState
# Revalidation provider calls reuse the Phase 4 provider-call state machine verbatim.
RevalidationCallState = ProviderCallState


class FindingLifecycleState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    RESOLUTION_INDETERMINATE = "resolution_indeterminate"


class OccurrenceDisposition(StrEnum):
    NEW = "new"
    EXACT_DUPLICATE = "exact_duplicate"
    SELECTED_PRIOR = "selected_prior"
    UNSELECTED_PRIOR = "unselected_prior"


class ThreadLinkState(StrEnum):
    PENDING_DISCOVERY = "pending_discovery"
    LINKED = "linked"
    NOT_THREADED = "not_threaded"
    DISCOVERY_INDETERMINATE = "discovery_indeterminate"


class RevalidationOutcome(StrEnum):
    FIXED = "fixed"
    STILL_PRESENT = "still_present"
    UNCERTAIN = "uncertain"


class RevalidationRunState(StrEnum):
    SELECTED = "selected"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class FindingRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    provider_id: str
    installation_id: str
    repository_id: str
    external_number: int = Field(gt=0)
    fingerprint_version: str
    primary_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    secondary_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str
    category: str
    rule: str
    symbol: str | None = None
    evidence: str = Field(min_length=1, max_length=512)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: FindingLifecycleState
    first_seen_run_id: str
    last_seen_run_id: str
    first_seen_head_sha: str
    last_seen_head_sha: str
    resolved_head_sha: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class FindingOccurrence(BaseModel):
    model_config = ConfigDict(frozen=True)
    finding_record_id: str
    review_run_id: str
    artifact_ordinal: int = Field(ge=0)
    observed_head_sha: str
    path: str
    line: int | None = Field(default=None, gt=0)
    disposition: OccurrenceDisposition
    publication_eligible: bool
    comment_linked: bool = False
    created_at: datetime


class ReviewThreadLink(BaseModel):
    model_config = ConfigDict(frozen=True)
    finding_record_id: str
    review_run_id: str | None = None
    provider_review_id: str | None = None
    provider_thread_node_id: str | None = None
    provider_comment_id: str | None = None
    marker_digest: str
    marker_version: str
    state: ThreadLinkState
    created_at: datetime
    updated_at: datetime


class FindingRevalidationRun(BaseModel):
    model_config = ConfigDict(frozen=True)
    review_run_id: str
    validated_head_sha: str
    selection_limit: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    unselected_count: int = Field(ge=0)
    policy_digest: str
    state: RevalidationRunState
    created_at: datetime
    updated_at: datetime


class FindingRevalidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    review_run_id: str
    finding_record_id: str
    outcome: RevalidationOutcome
    reason_code: str
    fetched_ref: str | None = None
    current_path: str | None = None
    created_at: datetime
    updated_at: datetime


class RevalidationCallIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    batch_ordinal: int = Field(ge=1)
    provider_id: str = Field(min_length=1, max_length=64)
    model_profile_id: str = Field(min_length=1, max_length=255)
    model_profile_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    estimated_input_tokens: int = Field(default=0, ge=0)


class RevalidationCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    identity: RevalidationCallIdentity
    state: RevalidationCallState
    created_at: datetime
    updated_at: datetime


class ThreadResolutionOperation(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    finding_record_id: str
    provider_thread_node_id: str
    validated_head_sha: str
    state: ThreadResolutionState
    attempt_count: int = Field(ge=0)
    provider_observed_state: str | None = None
    terminal_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class SelectedPriorFinding(BaseModel):
    """A durable selection decision for a prior open finding."""

    model_config = ConfigDict(frozen=True)
    finding_record_id: str
    primary_fingerprint: str
    path: str
    rule: str
    symbol: str | None
    evidence: str
    selected: bool
    reason_code: str


class ReviewThread(BaseModel):
    """Provider-neutral review thread discovered through the SCM adapter."""

    model_config = ConfigDict(frozen=True)
    thread_node_id: str
    is_resolved: bool
    review_id: str | None = None
    comment_ids: tuple[str, ...] = ()


class ThreadDiscoveryResult(BaseModel):
    """Bounded, provider-neutral marker-discovery outcome.

    ``match_count`` counts threads whose comments contain the exact per-finding marker.
    A ``collection_complete`` zero match is actionable; capped or incomplete enumeration is
    indeterminate and never claims a thread is absent.
    """

    model_config = ConfigDict(frozen=True)
    match_count: int = Field(ge=0)
    collection_complete: bool
    pages_inspected: int = Field(ge=0)
    items_inspected: int = Field(ge=0)
    provider_limit_reached: bool = False
    service_limit_reached: bool = False
    thread_node_ids: tuple[str, ...] = ()
    resolved_states: tuple[bool, ...] = ()
    review_ids: tuple[str, ...] = ()
    comment_ids: tuple[str, ...] = ()

    @property
    def actionable_zero(self) -> bool:
        return self.collection_complete and self.match_count == 0


class ThreadReadResult(BaseModel):
    """Bounded, complete reread of a single thread by node ID."""

    model_config = ConfigDict(frozen=True)
    found: bool
    is_resolved: bool
    collection_complete: bool
    thread_node_id: str | None = None


class RevalidationUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: str
    usage: TokenUsage | None = None


class FindingRetentionResult(BaseModel):
    """Counts from one bounded operator-requested closed-finding pruning batch."""

    model_config = ConfigDict(frozen=True)
    findings: int = Field(ge=0)
    occurrences: int = Field(ge=0)
    links: int = Field(ge=0)
    operations: int = Field(ge=0)
    results: int = Field(ge=0)
