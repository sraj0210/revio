"""Minimal private GitHub webhook DTOs."""

from pydantic import BaseModel, ConfigDict


class GitHubWebhookInstallation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int


class GitHubWebhookRepository(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    full_name: str


class GitHubWebhookRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str


class GitHubWebhookPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    number: int
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
