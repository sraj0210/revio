"""Minimal private GitHub webhook DTOs."""

from pydantic import BaseModel, ConfigDict, Field


class GitHubWebhookInstallation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)


class GitHubWebhookRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    full_name: str = Field(min_length=3, max_length=512)


class GitHubWebhookRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=1, max_length=128)


class GitHubWebhookPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int = Field(gt=0)
    base: GitHubWebhookRef
    head: GitHubWebhookRef


class GitHubInstallationWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    installation: GitHubWebhookInstallation


class GitHubPullRequestWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    installation: GitHubWebhookInstallation
    repository: GitHubWebhookRepository
    pull_request: GitHubWebhookPullRequest
