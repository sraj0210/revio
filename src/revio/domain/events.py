"""Provider-neutral webhook event models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, ProviderId, RepositoryRef


class ReviewEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: ProviderId
    delivery_identity: str = Field(min_length=1, max_length=320)
    semantic_identity: str = Field(min_length=1, max_length=700)
    event_type: Literal["installation", "change_request"]
    trigger: str = Field(min_length=1, max_length=64)
    installation: InstallationRef
    repository: RepositoryRef | None = None
    change_request: ChangeRequestTarget | None = None
    event_head_sha: str | None = None
    event_base_sha: str | None = None


class WebhookNormalizationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    disposition: Literal["accepted", "ignored"]
    event: ReviewEvent | None = None
    reason: str | None = None
