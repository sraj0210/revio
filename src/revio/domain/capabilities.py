"""Provider-wide and resolved model capability descriptions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from revio.domain.identifiers import ModelAlias, ProviderId


class SCMCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)
    inline_comments: bool = False
    summary_comments: bool = False
    threaded_replies: bool = False
    resolvable_threads: bool = False
    reviewer_assignment_events: bool = False
    review_rerequest_events: bool = False
    check_runs: bool = False
    commit_statuses: bool = False
    suggested_changes: bool = False
    repository_file_access: bool = False
    tree_access: bool = False
    webhook_event_uuids: bool = False
    installation_authentication: bool = False


class AIProviderCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)
    usage_reporting: bool = False
    streaming_transport: bool = False
    image_transport: bool = False
    retryable_error_classification: bool = False


class ResolvedModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    alias: ModelAlias
    provider_id: ProviderId
    provider_model_id: str = Field(min_length=1, max_length=255)
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    structured_output: Literal["none", "json", "json_schema"] = "none"
    native_tools: bool = False
    prompt_caching: bool = False
    image_input: bool = False
    allowed_review_modes: frozenset[Literal["diff_only", "agent"]] = frozenset({"diff_only"})
    profile_version: str = Field(default="1", min_length=1, max_length=64)
    thinking: Literal["disabled", "enabled", "adaptive"] = "disabled"
    use_default_sampling: bool = True
    token_counting: bool = False
