"""Private GitHub REST response DTOs."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class GitHubTokenDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: SecretStr = Field(min_length=1)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiry_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timezone is required")
        return value


class GitHubRefDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=1, max_length=128)
    ref: str


class GitHubPullRequestDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int = Field(gt=0)
    title: str
    body: str | None = None
    state: Literal["open", "closed"]
    draft: bool = False
    merged: bool = False
    changed_files: int | None = Field(default=None, ge=0)
    base: GitHubRefDTO
    head: GitHubRefDTO


class GitHubFileDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename: str = Field(min_length=1)
    previous_filename: str | None = None
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    patch: str | None = None
    additions: int | None = Field(default=None, ge=0)
    deletions: int | None = Field(default=None, ge=0)
    changes: int | None = Field(default=None, ge=0)


class GitHubContentDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    content: str | None = None
    encoding: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha: str = Field(min_length=1, max_length=128)


class GitHubTreeEntryDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(min_length=1)
    mode: str
    type: Literal["blob", "tree", "commit"]
    sha: str = Field(min_length=1, max_length=128)
    size: int | None = Field(default=None, ge=0)


class GitHubTreeDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=1, max_length=128)
    truncated: bool = False
    tree: list[GitHubTreeEntryDTO]


class GitHubCommitDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str
    commit: dict[str, object]
