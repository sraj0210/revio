"""Private GitHub REST response DTOs."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class GitHubTokenDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str
    expires_at: datetime


class GitHubRefDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str
    ref: str


class GitHubPullRequestDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int
    title: str
    body: str | None = None
    state: Literal["open", "closed"]
    draft: bool = False
    merged: bool = False
    base: GitHubRefDTO
    head: GitHubRefDTO


class GitHubFileDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename: str
    previous_filename: str | None = None
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    patch: str | None = None


class GitHubContentDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    content: str | None = None
    encoding: str | None = None
    size: int | None = None
    sha: str


class GitHubTreeEntryDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    mode: str
    type: Literal["blob", "tree", "commit"]
    sha: str
    size: int | None = None


class GitHubTreeDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str
    truncated: bool = False
    tree: list[GitHubTreeEntryDTO]


class GitHubCommitDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str
    commit: dict[str, object]
