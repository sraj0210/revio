"""Phase 3 current-head validation; no diff or source retrieval."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from revio.application.queue.retry import retry_delay
from revio.config.queue import QueueSettings
from revio.domain.models import ChangeRequest
from revio.domain.queue import InstallationStatus, JobLease
from revio.errors import LeaseLostError, ProviderTransientError
from revio.ports.credentials import InstallationCredentialCachePort
from revio.ports.observability import QueueMetricsPort
from revio.ports.persistence import QueueRepository
from revio.ports.time import Clock, RandomSource
from revio.registries import ProviderRegistry


class ReviewJobExecutorPort(Protocol):
    async def execute(
        self, lease: JobLease, current: ChangeRequest, *, now: datetime | None = None
    ) -> str: ...


class QueueProcessor:
    def __init__(
        self,
        repository: QueueRepository,
        providers: ProviderRegistry,
        token_cache: InstallationCredentialCachePort,
        settings: QueueSettings,
        metrics: QueueMetricsPort | None = None,
        clock: Clock | None = None,
        random_source: RandomSource | None = None,
        review_executor: ReviewJobExecutorPort | None = None,
    ) -> None:
        self._repository = repository
        self._providers = providers
        self._token_cache = token_cache
        self._settings = settings
        self._metrics = metrics
        self._clock = clock
        self._random = random_source
        self._review_executor = review_executor

    async def _require_owned(self, changed: bool) -> None:
        if not changed:
            if self._metrics is not None:
                self._metrics.increment("lease_lost")
            raise LeaseLostError("queue lease ownership was lost")

    async def process(self, lease: JobLease) -> None:
        now = self._clock.now() if self._clock is not None else datetime.now(UTC)
        event = lease.job.event
        await self._require_owned(await self._repository.heartbeat(lease, now))
        installation_id = event.installation.external_id
        state = await self._repository.installation_state(str(event.provider_id), installation_id)
        if state is not None and state.state in {
            InstallationStatus.SUSPENDED,
            InstallationStatus.DELETED,
        }:
            await self._token_cache.invalidate(event.installation)
            await self._require_owned(
                await self._repository.terminate(
                    lease, "cancelled", f"installation_{state.state}", now
                )
            )
            if self._metrics is not None:
                self._metrics.increment("installation_state_block", outcome=str(state.state))
            return
        if event.change_request is None or event.event_head_sha is None:
            await self._require_owned(
                await self._repository.terminate(lease, "dead", "invalid_job", now)
            )
            return
        try:
            current = await self._providers.scm(event.provider_id).reader.get_change_request(
                event.change_request
            )
        except ProviderTransientError as error:
            retry_after = getattr(error, "retry_after_seconds", None)
            delay = retry_delay(
                lease.attempt_number,
                self._settings,
                retry_after=retry_after,
                random_value=(self._random.uniform(0.0, 1.0) if self._random is not None else None),
            )
            await self._require_owned(
                await self._repository.retry(
                    lease,
                    available_at=now + timedelta(seconds=delay),
                    error_class=type(error).__name__,
                    error_message="provider read temporarily unavailable",
                    now=now,
                )
            )
            return
        except Exception:
            await self._require_owned(
                await self._repository.terminate(lease, "dead", "provider_read_failed", now)
            )
            return
        if current.state in {"closed", "merged"}:
            await self._require_owned(
                await self._repository.terminate(lease, "cancelled", "change_request_closed", now)
            )
        elif current.head_sha != event.event_head_sha:
            await self._require_owned(
                await self._repository.terminate(lease, "superseded", "stale_head", now)
            )
        elif self._review_executor is None:
            await self._require_owned(
                await self._repository.complete(
                    lease, head_sha=current.head_sha, base_sha=current.base_sha, now=now
                )
            )
        else:
            try:
                outcome = await self._review_executor.execute(lease, current, now=now)
            except ProviderTransientError as error:
                delay = retry_delay(
                    lease.attempt_number,
                    self._settings,
                    retry_after=getattr(error, "retry_after_seconds", None),
                    random_value=(
                        self._random.uniform(0.0, 1.0) if self._random is not None else None
                    ),
                )
                await self._require_owned(
                    await self._repository.retry(
                        lease,
                        available_at=now + timedelta(seconds=delay),
                        error_class=type(error).__name__,
                        error_message="review provider temporarily unavailable",
                        now=now,
                    )
                )
                return
            except Exception:
                await self._require_owned(
                    await self._repository.terminate(lease, "dead", "review_execution_failed", now)
                )
                return
            if outcome == "superseded":
                await self._require_owned(
                    await self._repository.terminate(
                        lease, "superseded", "stale_head_during_review", now
                    )
                )
            else:
                await self._require_owned(
                    await self._repository.complete(
                        lease, head_sha=current.head_sha, base_sha=current.base_sha, now=now
                    )
                )
