"""Map private GitHub payloads to provider-neutral events."""

from typing import Any

from pydantic import ValidationError

from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.dto.webhook import (
    GitHubInstallationWebhook,
    GitHubPullRequestWebhook,
)
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.domain.events import ReviewEvent, WebhookNormalizationResult
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef, RepositoryRef


def normalize_webhook(
    event_name: str, delivery_id: str, payload: dict[str, Any]
) -> WebhookNormalizationResult:
    try:
        if event_name == "installation":
            dto = GitHubInstallationWebhook.model_validate(payload)
            if dto.action not in {"created", "deleted", "suspend", "unsuspend"}:
                return WebhookNormalizationResult(
                    disposition="ignored", reason="unsupported action"
                )
            installation = InstallationRef(
                provider_id=GITHUB_PROVIDER_ID, external_id=str(dto.installation.id)
            )
            event = ReviewEvent(
                provider_id=GITHUB_PROVIDER_ID,
                delivery_identity=f"github:{delivery_id}",
                semantic_identity=f"github:{dto.installation.id}:installation:{dto.action}:{delivery_id}",
                event_type="installation",
                trigger=dto.action,
                installation=installation,
                provider_updated_at=dto.installation.updated_at,
            )
            return WebhookNormalizationResult(disposition="accepted", event=event)
        if event_name == "pull_request":
            dto = GitHubPullRequestWebhook.model_validate(payload)
            if dto.action not in {"opened", "reopened", "synchronize"}:
                return WebhookNormalizationResult(
                    disposition="ignored", reason="unsupported action"
                )
            owner, separator, name = dto.repository.full_name.partition("/")
            if not separator or name != dto.repository.name:
                raise GitHubResponseError("invalid GitHub repository identity")
            installation = InstallationRef(
                provider_id=GITHUB_PROVIDER_ID, external_id=str(dto.installation.id)
            )
            repository = RepositoryRef(
                installation=installation,
                external_id=str(dto.repository.id),
                owner=owner,
                name=name,
            )
            target = ChangeRequestTarget(
                repository=repository, external_number=dto.pull_request.number
            )
            if dto.action == "reopened":
                semantic_identity = (
                    f"v1:github:{dto.installation.id}:{dto.repository.id}:change-request:"
                    f"{dto.pull_request.number}:head:{dto.pull_request.head.sha}:"
                    f"reopened:{delivery_id}"
                )
            else:
                semantic_identity = (
                    f"v1:github:{dto.installation.id}:{dto.repository.id}:change-request:"
                    f"{dto.pull_request.number}:head:{dto.pull_request.head.sha}:review"
                )
            event = ReviewEvent(
                provider_id=GITHUB_PROVIDER_ID,
                delivery_identity=f"github:{delivery_id}",
                semantic_identity=semantic_identity,
                event_type="change_request",
                trigger=dto.action,
                installation=installation,
                repository=repository,
                change_request=target,
                event_head_sha=dto.pull_request.head.sha,
                event_base_sha=dto.pull_request.base.sha,
            )
            return WebhookNormalizationResult(disposition="accepted", event=event)
    except ValidationError as error:
        raise GitHubResponseError("invalid GitHub webhook payload") from error
    return WebhookNormalizationResult(disposition="ignored", reason="unsupported event")
