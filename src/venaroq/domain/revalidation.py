"""Provider-neutral finding-revalidation request/result contract.

A revalidation call asks only whether an exact earlier finding is fixed, still present, or
uncertain given the current file content at the exact latest head. It can never create new
review findings or free-form thread text. Every requested finding ID must appear exactly once
in the result; any missing, duplicate, unknown, or malformed result makes the whole batch
uncertain.
"""

from pydantic import BaseModel, ConfigDict, Field

from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.lifecycle import RevalidationOutcome
from venaroq.domain.models import TokenUsage


class RevalidationRequestItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    finding_id: str = Field(min_length=1, max_length=128)
    path: str
    rule: str
    symbol: str | None = None
    evidence: str = Field(min_length=1, max_length=512)
    current_file_content: str


class FindingRevalidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    head_sha: str = Field(min_length=1, max_length=128)
    items: tuple[RevalidationRequestItem, ...] = Field(min_length=1)
    model_profile: ResolvedModelProfile


class RevalidationResultItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    finding_id: str = Field(min_length=1, max_length=128)
    outcome: RevalidationOutcome
    current_path: str | None = None


class FindingRevalidationBatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    results: tuple[RevalidationResultItem, ...]
    usage: TokenUsage = TokenUsage()
