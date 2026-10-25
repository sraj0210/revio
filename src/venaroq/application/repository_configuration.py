"""Trusted-base Phase 5 repository configuration resolution."""

import hashlib
from datetime import datetime
from typing import Protocol

from venaroq.config.review import ReviewSettings
from venaroq.domain.configuration import (
    MAX_CONFIG_BYTES,
    ConfigurationResolution,
    ConfigurationWarningCode,
    EffectiveReviewPolicy,
    parse_repository_configuration,
    resolve_policy,
)
from venaroq.domain.models import ChangeRequest
from venaroq.domain.queue import JobLease
from venaroq.errors import ProviderTransientError
from venaroq.ports.scm import RepositoryContentReadPort


class ConfigurationRepository(Protocol):
    async def get(self, job_id: str) -> ConfigurationResolution | None: ...
    async def persist(self, resolution: ConfigurationResolution) -> ConfigurationResolution: ...


class PolicyReviewExecutor(Protocol):
    async def execute(
        self,
        lease: JobLease,
        current: ChangeRequest,
        *,
        now: datetime | None = None,
        policy: EffectiveReviewPolicy | None = None,
    ) -> str: ...


def repository_identity(lease: JobLease) -> str:
    event = lease.job.event
    repository = event.repository or (
        event.change_request.repository if event.change_request is not None else None
    )
    if repository is None:
        raise ValueError("repository identity is unavailable")
    return f"{event.provider_id}:{repository.installation.external_id}:{repository.external_id}"


class RepositoryConfigurationExecutor:
    def __init__(
        self,
        repository: ConfigurationRepository,
        content: RepositoryContentReadPort,
        executor: PolicyReviewExecutor,
        settings: ReviewSettings,
    ) -> None:
        self._repository = repository
        self._content = content
        self._executor = executor
        self._settings = settings

    def _defaults(
        self, warnings: tuple[ConfigurationWarningCode, ...] = ()
    ) -> EffectiveReviewPolicy:
        return resolve_policy(
            service_summary=self._settings.review_summary_confidence,
            service_inline=self._settings.review_inline_confidence,
            protected_exclusions=self._settings.protected_exclusions,
            warning_codes=warnings,
        )

    async def execute(
        self, lease: JobLease, current: ChangeRequest, *, now: datetime | None = None
    ) -> str:
        if current.draft and not self._settings.review_drafts_enabled:
            return "draft_disabled"
        identity = repository_identity(lease)
        if identity not in self._settings.rollout_repositories:
            return "rollout_disabled"
        existing = await self._repository.get(lease.job.id)
        if existing is not None:
            if not existing.policy.enabled:
                return "repository_disabled"
            return await self._executor.execute(lease, current, now=now, policy=existing.policy)
        current_time = now or datetime.now().astimezone()
        event = lease.job.event
        target = event.change_request
        repository = event.repository or (target.repository if target is not None else None)
        if target is None or repository is None:
            raise ValueError("configuration target is unavailable")
        source: bytes | None = None
        status = "missing"
        schema_version: int | None = None
        source_digest: str | None = None
        try:
            text = await self._content.get_file(target, ".venaroq.yml", current.base_sha)
            source = text.encode("utf-8") if text is not None else None
            if source is None:
                policy = self._defaults()
            else:
                source_digest = hashlib.sha256(source).hexdigest()
                if len(source) > MAX_CONFIG_BYTES:
                    status = "invalid"
                    raise _ResolvedOversizedError(
                        self._defaults((ConfigurationWarningCode.OVERSIZED,))
                    )
                else:
                    parsed = parse_repository_configuration(source)
                policy = resolve_policy(
                    service_summary=self._settings.review_summary_confidence,
                    service_inline=self._settings.review_inline_confidence,
                    protected_exclusions=self._settings.protected_exclusions,
                    repository=parsed,
                    allow_repository_optout=self._settings.review_allow_repository_optout,
                )
                schema_version = parsed.version
                status = (
                    "optout_denied"
                    if ConfigurationWarningCode.OPTOUT_DENIED in policy.warning_codes
                    else "valid"
                )
        except _ResolvedOversizedError as resolved:
            policy = resolved.policy
        except ProviderTransientError:
            raise
        except ValueError:
            status = "invalid"
            policy = self._defaults((ConfigurationWarningCode.INVALID,))
        except Exception:
            status = "unavailable"
            policy = self._defaults((ConfigurationWarningCode.UNAVAILABLE,))
        resolution = ConfigurationResolution(
            job_id=lease.job.id,
            provider_id=str(event.provider_id),
            installation_id=repository.installation.external_id,
            repository_id=repository.external_id,
            base_sha=current.base_sha,
            status=status,
            schema_version=schema_version,
            source_digest=source_digest,
            policy=policy,
            created_at=current_time,
            updated_at=current_time,
        )
        durable = await self._repository.persist(resolution)
        if not durable.policy.enabled:
            return "repository_disabled"
        return await self._executor.execute(lease, current, now=now, policy=durable.policy)


class _ResolvedOversizedError(Exception):
    def __init__(self, policy: EffectiveReviewPolicy) -> None:
        super().__init__("oversized configuration was resolved to defaults")
        self.policy = policy
