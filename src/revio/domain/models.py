"""Normalized change-request and review models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ChangeRequestTarget


class DiffLine(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str
    side: Literal["old", "new"]
    old_line: int | None = Field(default=None, gt=0)
    new_line: int | None = Field(default=None, gt=0)


class DiffFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    old_path: str | None = None
    new_path: str
    status: Literal["added", "modified", "deleted", "renamed"]
    lines: tuple[DiffLine, ...] = ()
    truncated: bool = False


class ChangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    target: ChangeRequestTarget
    title: str
    description: str = ""
    base_sha: str = Field(min_length=1, max_length=128)
    head_sha: str = Field(min_length=1, max_length=128)
    state: Literal["open", "closed", "merged"] = "open"
    draft: bool = False


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    path: str
    line: int | None = Field(default=None, gt=0)


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


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    findings: tuple[Finding, ...] = ()
    summary: str
    partial: bool = False
    fallback_mode: bool = False
    usage: TokenUsage = TokenUsage()
