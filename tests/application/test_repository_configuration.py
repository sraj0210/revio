"""Trusted-base loading, rollout, opt-out, and restart behavior."""

from datetime import UTC, datetime, timedelta

import pytest

from venaroq.application.repository_configuration import RepositoryConfigurationExecutor
from venaroq.config.review import ReviewSettings
from venaroq.domain.configuration import ConfigurationResolution, EffectiveReviewPolicy
from venaroq.domain.events import ReviewEvent
from venaroq.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ProviderId,
    RepositoryRef,
)
from venaroq.domain.models import ChangeRequest, TreeCollection
from venaroq.domain.queue import JobLease, QueueJob, QueueState


class MemoryConfigurations:
    def __init__(self) -> None:
        self.value: ConfigurationResolution | None = None
        self.persisted = 0

    async def get(self, job_id: str) -> ConfigurationResolution | None:
        assert job_id == "job"
        return self.value

    async def persist(self, resolution: ConfigurationResolution) -> ConfigurationResolution:
        self.persisted += 1
        self.value = self.value or resolution
        return self.value


class Content:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.refs: list[str] = []

    async def get_file(self, target: ChangeRequestTarget, path: str, ref: str) -> str | None:
        assert path == ".venaroq.yml"
        self.refs.append(ref)
        return self.value

    async def get_tree(self, target: ChangeRequestTarget, path: str, ref: str) -> TreeCollection:
        raise AssertionError("tree reads are not used")


class Executor:
    def __init__(self) -> None:
        self.policies: list[EffectiveReviewPolicy] = []

    async def execute(
        self,
        lease: JobLease,
        current: ChangeRequest,
        *,
        now: datetime | None = None,
        policy: EffectiveReviewPolicy | None = None,
    ) -> str:
        assert policy is not None
        self.policies.append(policy)
        return "completed"


def _context() -> tuple[JobLease, ChangeRequest, str]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    provider = ProviderId(value="github")
    installation = InstallationRef(provider_id=provider, external_id="123")
    repository = RepositoryRef(
        installation=installation, external_id="456", owner="owner", name="repo"
    )
    target = ChangeRequestTarget(repository=repository, external_number=7)
    event = ReviewEvent(
        provider_id=provider,
        delivery_identity="delivery",
        semantic_identity="semantic",
        event_type="change_request",
        trigger="opened",
        installation=installation,
        repository=repository,
        change_request=target,
        event_head_sha="head",
        event_base_sha="base",
    )
    job = QueueJob(
        id="job",
        provider_id="github",
        job_type="review",
        semantic_identity="semantic",
        event=event,
        state=QueueState.RUNNING,
        attempt_count=1,
        max_attempts=5,
        available_at=now,
        lease_owner="worker",
        lease_expires_at=now + timedelta(minutes=1),
    )
    return (
        JobLease(
            job=job, worker_id="worker", attempt_number=1, expires_at=now + timedelta(minutes=1)
        ),
        ChangeRequest(target=target, title="Change", base_sha="base", head_sha="head"),
        "github:123:456",
    )


@pytest.mark.asyncio
async def test_exact_base_resolution_is_durable_and_restart_does_not_refetch() -> None:
    lease, current, identity = _context()
    configurations = MemoryConfigurations()
    content = Content("version: 1\nreview:\n  focus_areas: [security]\n  exclude: [docs/**]\n")
    executor = Executor()
    service = RepositoryConfigurationExecutor(
        configurations,
        content,
        executor,
        ReviewSettings(review_rollout_repositories=identity),
    )
    assert await service.execute(lease, current) == "completed"
    content.value = "version: 1\nenabled: false\n"
    assert await service.execute(lease, current) == "completed"
    assert content.refs == ["base"]
    assert configurations.persisted == 1
    assert executor.policies[0] == executor.policies[1]
    assert executor.policies[0].focus_areas == ("security",)


@pytest.mark.asyncio
async def test_rollout_and_permitted_optout_block_all_downstream_work() -> None:
    lease, current, identity = _context()
    content = Content("version: 1\nenabled: false\n")
    executor = Executor()
    configurations = MemoryConfigurations()
    disabled = RepositoryConfigurationExecutor(configurations, content, executor, ReviewSettings())
    assert await disabled.execute(lease, current) == "rollout_disabled"
    assert content.refs == [] and executor.policies == []

    opted_out = RepositoryConfigurationExecutor(
        configurations,
        content,
        executor,
        ReviewSettings(
            review_rollout_repositories=identity,
            review_allow_repository_optout=True,
        ),
    )
    assert await opted_out.execute(lease, current) == "repository_disabled"
    assert content.refs == ["base"] and executor.policies == []


@pytest.mark.asyncio
async def test_invalid_configuration_uses_defaults_with_bounded_warning() -> None:
    lease, current, identity = _context()
    executor = Executor()
    service = RepositoryConfigurationExecutor(
        MemoryConfigurations(),
        Content("version: 1\nreview: {exclude: ['../secret']}\n"),
        executor,
        ReviewSettings(review_rollout_repositories=identity),
    )
    assert await service.execute(lease, current) == "completed"
    assert executor.policies[0].warning_text is not None
    assert executor.policies[0].exclusions == (".venaroq.yml",)
