"""Normalized change-request and review models."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.identifiers import ChangeRequestTarget

EVIDENCE_MAX_CODE_POINTS = 512
EVIDENCE_MAX_BYTES = 2 * 1024
SYMBOL_MAX_CODE_POINTS = 256


class DiffLine(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    side: Literal["old", "new", "context"]
    old_line: int | None = Field(default=None, gt=0)
    new_line: int | None = Field(default=None, gt=0)


class DiffFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    old_path: str | None = None
    new_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed", "copied"]
    lines: tuple[DiffLine, ...] = ()
    additions: int | None = Field(default=None, ge=0)
    deletions: int | None = Field(default=None, ge=0)
    changes: int | None = Field(default=None, ge=0)
    patch_state: Literal[
        "complete",
        "missing",
        "malformed",
        "provider_truncated",
        "no_textual_patch_unknown_reason",
    ] = "complete"


class CollectionCompleteness(BaseModel):
    """Explicitly prevents partial provider data from appearing complete."""

    model_config = ConfigDict(frozen=True)
    status: Literal[
        "complete", "provider_truncated", "service_page_limit", "service_item_limit"
    ] = "complete"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


class DiffCollection(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[DiffFile, ...] = ()
    completeness: CollectionCompleteness = CollectionCompleteness()
    expected_file_count: int | None = Field(default=None, ge=0)


class RepositoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    entry_type: Literal["blob", "tree", "commit"]
    sha: str = Field(min_length=1, max_length=128)
    size: int | None = Field(default=None, ge=0)


class TreeCollection(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: tuple[RepositoryEntry, ...] = ()
    completeness: CollectionCompleteness = CollectionCompleteness()


class ChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    target: ChangeRequestTarget
    title: str
    description: str = ""
    base_sha: str = Field(min_length=1, max_length=128)
    head_sha: str = Field(min_length=1, max_length=128)
    state: Literal["open", "closed", "merged"] = "open"
    draft: bool = False


class FindingIdentity(BaseModel):
    """Bounded, structured identity material for durable finding fingerprints.

    Generated title, explanation, remediation, confidence wording, and line are never
    identity inputs. Only the normalized rule, optional symbol, and the exact bounded
    evidence snippet (validated against the submitted diff) participate in the primary
    fingerprint.
    """

    model_config = ConfigDict(frozen=True)
    rule: Annotated[
        str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*$")
    ]
    symbol: str | None = Field(default=None, min_length=1, max_length=SYMBOL_MAX_CODE_POINTS)
    evidence: str = Field(min_length=1, max_length=EVIDENCE_MAX_CODE_POINTS)

    @model_validator(mode="after")
    def validate_bounds(self) -> "FindingIdentity":
        if len(self.evidence.encode("utf-8")) > EVIDENCE_MAX_BYTES:
            raise ValueError("finding evidence exceeds the UTF-8 byte ceiling")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    path: str
    line: int | None = Field(default=None, gt=0)
    inline_eligible: bool = False
    identity: FindingIdentity | None = None

    @property
    def is_inline(self) -> bool:
        return self.line is not None


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)
    uncached_input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    change_request: ChangeRequest
    diff_files: tuple[DiffFile, ...]
    model_profile: ResolvedModelProfile
    focus_areas: tuple[str, ...] = ()


AgentToolName = Literal["get_file", "get_tree"]


class ConsultedFile(BaseModel):
    """One repository object consulted during agent-mode review, for verification provenance."""

    model_config = ConfigDict(frozen=True)
    path: str = Field(min_length=1, max_length=1000)
    ref: str = Field(min_length=1, max_length=128)
    kind: AgentToolName


class AgentProvenance(BaseModel):
    """What agent-mode review consulted, and whether exploration was complete.

    ``complete=False`` means a limit was reached or a request was refused, so the context the model
    saw is partial. Contains only paths and commit SHAs — never any fetched content.
    """

    model_config = ConfigDict(frozen=True)
    consulted: tuple[ConsultedFile, ...] = ()
    complete: bool = True

    def note(self) -> str:
        """A compact, human-readable provenance line for the published review."""
        if not self.consulted:
            return ""
        refs = {item.ref for item in self.consulted}
        ref = next(iter(refs)) if len(refs) == 1 else "the pull request"
        suffix = "" if self.complete else " (context was limited)"
        count = len(self.consulted)
        plural = "file" if count == 1 else "files"
        return f"Verified against {count} repository {plural} at {ref}{suffix}."


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    findings: tuple[Finding, ...] = ()
    summary: str
    partial: bool = False
    fallback_mode: bool = False
    usage: TokenUsage = TokenUsage()
    provenance: AgentProvenance | None = None
