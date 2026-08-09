"""Durable provider-neutral Phase 4 review lifecycle models."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from revio.domain.models import Finding, TokenUsage


class ProviderCallState(StrEnum):
    RESERVED = "reserved"
    ATTEMPT_STARTED = "attempt_started"
    RESPONSE_OBSERVED = "response_observed"
    AMBIGUOUS = "ambiguous"
    RETRYABLE_REJECTED = "retryable_rejected"
    TERMINAL_REJECTED = "terminal_rejected"
    KNOWN_REJECTED = "known_rejected"  # Read compatibility; never authorizes retry.
    COMPLETED = "completed"


class WriteOperationState(StrEnum):
    RESERVED_UNATTEMPTED = "reserved_unattempted"
    ATTEMPT_STARTED = "attempt_started"
    KNOWN_REJECTED = "known_rejected"
    AMBIGUOUS = "ambiguous"
    RECONCILED = "reconciled"
    COMPLETED = "completed"
    INTEGRITY_FAILED = "integrity_failed"


class ReviewRunState(StrEnum):
    GENERATION_PENDING = "generation_pending"
    GENERATION_ATTEMPTED = "generation_attempted"
    ARTIFACT_DURABLE = "artifact_durable"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    SUPERSEDED = "superseded"
    PUBLICATION_INDETERMINATE = "publication_indeterminate"
    CHECK_RUN_INDETERMINATE = "check_run_indeterminate"


class PartialReason(StrEnum):
    INPUT_INCOMPLETE = "input_incomplete"
    INPUT_TRUNCATED = "input_truncated"
    PROVIDER_REFUSAL = "provider_refusal"
    PROVIDER_OUTPUT_TRUNCATED = "provider_output_truncated"
    OUTPUT_INVALID = "output_invalid"
    REPAIR_FAILED = "repair_failed"
    PROVIDER_CALL_AMBIGUOUS = "provider_call_ambiguous"
    RESPONSE_RECOVERY_UNAVAILABLE = "response_recovery_unavailable"
    ANCHOR_INVALID = "anchor_invalid"
    ANCHOR_RACE = "anchor_race"
    PUBLICATION_INDETERMINATE = "publication_indeterminate"
    SUPERSEDED = "superseded"
    FINDINGS_TRUNCATED = "findings_truncated"


class DiffEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    expected_file_count: int = Field(ge=0)
    returned_unique_file_count: int = Field(ge=0)
    provider_collection_complete: bool
    patch_complete: bool
    service_truncation_reasons: tuple[str, ...] = ()
    file_count: int = Field(ge=0)
    line_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)

    @property
    def partial(self) -> bool:
        return (
            not self.provider_collection_complete
            or not self.patch_complete
            or self.expected_file_count != self.returned_unique_file_count
            or bool(self.service_truncation_reasons)
        )


class ProviderCallIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=1, max_length=128)
    review_run_id: str = Field(min_length=1, max_length=128)
    call_kind: Literal["initial", "repair"]
    call_ordinal: int = Field(ge=1)
    provider_id: str = Field(min_length=1, max_length=64)
    model_profile_id: str = Field(min_length=1, max_length=255)
    model_profile_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    estimated_input_tokens: int = Field(default=0, ge=0)


class ProviderCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    identity: ProviderCallIdentity
    state: ProviderCallState
    created_at: datetime
    updated_at: datetime


class UsageDisposition(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["known", "unknown"]
    usage: TokenUsage | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> "UsageDisposition":
        if (self.status == "known") != (self.usage is not None):
            raise ValueError("known usage requires values and unknown usage forbids values")
        return self


class NormalizedReviewArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    review_run_id: str = Field(min_length=1, max_length=128)
    artifact_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    model_profile_id: str = Field(min_length=1, max_length=255)
    model_profile_version: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=8000)
    findings: tuple[Finding, ...] = Field(default=(), max_length=100)
    partial: bool = False
    reason_codes: tuple[PartialReason, ...] = ()
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_partial(self) -> "NormalizedReviewArtifact":
        if self.partial and not self.reason_codes:
            raise ValueError("partial artifacts require a stable reason")
        if not self.partial and self.reason_codes:
            raise ValueError("complete artifacts cannot contain partial reasons")
        return self


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    match_count: int = Field(ge=0)
    collection_complete: bool
    pages_inspected: int = Field(ge=0)
    items_inspected: int = Field(ge=0)
    provider_limit_reached: bool = False
    service_limit_reached: bool = False
    provider_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_matches(self) -> "ReconciliationResult":
        if self.match_count != len(self.provider_ids):
            raise ValueError("match count must equal provider identity count")
        if self.collection_complete and (self.provider_limit_reached or self.service_limit_reached):
            raise ValueError("complete reconciliation cannot have a reached limit")
        return self

    @property
    def actionable_zero(self) -> bool:
        return self.collection_complete and self.match_count == 0


class PublishedReview(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_review_id: str = Field(min_length=1, max_length=128)


class ReviewStatusDetails(BaseModel):
    model_config = ConfigDict(frozen=True)
    external_id: str = Field(min_length=1, max_length=128)
    head_sha: str = Field(min_length=1, max_length=128)
    name: str = "Revio review"
    status: Literal["queued", "in_progress", "completed"]
    conclusion: Literal["success", "neutral", "failure"] | None = None
    summary: str = Field(min_length=1, max_length=1000)


class ReviewRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    job_id: str
    state: ReviewRunState
    validated_head_sha: str
    validated_base_sha: str
    check_run_external_id: str
    provider_check_run_id: str | None = None
    terminal_reason: str | None = None


class WriteOperationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    review_run_id: str
    state: WriteOperationState
    head_sha: str
    attempt_count: int
    provider_id: str | None = None
    operation_key: str | None = None
    marker: str | None = None
    marker_key_id: str | None = None
    external_id: str | None = None
    terminal_reason: str | None = None
