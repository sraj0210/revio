"""Compatibility facade over cohesive SQLite persistence components."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiosqlite

from revio.adapters.observability import InMemoryQueueMetrics
from revio.adapters.persistence.sqlite.connection import (
    CoordinationHook,
    FailureHook,
    SQLiteConnectionPolicy,
)
from revio.adapters.persistence.sqlite.ingress import SQLiteIngressRepository
from revio.adapters.persistence.sqlite.installations import SQLiteInstallationRepository
from revio.adapters.persistence.sqlite.queue import SQLiteQueueRepository
from revio.adapters.persistence.sqlite.readiness import SQLiteReadinessRepository
from revio.adapters.persistence.sqlite.retention import SQLiteTerminalRetentionRepository
from revio.adapters.persistence.sqlite.reviews import SQLiteReviewRepository
from revio.adapters.persistence.sqlite.schema import SCHEMA_REVISION, SCHEMA_SQL
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.events import WebhookNormalizationResult
from revio.domain.queue import (
    IngressReceipt,
    InstallationState,
    JobLease,
    QueueJob,
    RetentionResult,
)
from revio.ports.observability import QueueMetricsPort


class SQLiteStore:
    """Thin facade retaining the provider-neutral persistence port surface."""

    def __init__(
        self,
        database: DatabaseSettings,
        queue: QueueSettings | None = None,
        *,
        metrics: QueueMetricsPort | None = None,
        failure_hook: FailureHook | None = None,
        coordination_hook: CoordinationHook | None = None,
    ) -> None:
        queue_settings = queue or QueueSettings()
        active_metrics = metrics or InMemoryQueueMetrics()
        self._connections = SQLiteConnectionPolicy(
            database,
            metrics=active_metrics,
            failure_hook=failure_hook,
            coordination_hook=coordination_hook,
        )
        self.metrics = active_metrics
        self._installations = SQLiteInstallationRepository(self._connections)
        self._ingress = SQLiteIngressRepository(
            self._connections, self._installations, queue_settings
        )
        self._queue = SQLiteQueueRepository(self._connections, self._installations, queue_settings)
        self._readiness = SQLiteReadinessRepository(self._connections)
        self.reviews = SQLiteReviewRepository(self._connections)
        self._retention = SQLiteTerminalRetentionRepository(
            self._connections, self._readiness.check_capabilities
        )

    @property
    def path(self) -> Path:
        return self._connections.path

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        async with self._connections.connect() as connection:
            yield connection

    async def initialize(self) -> None:
        """Create and stamp the schema for isolated tests; production uses Alembic."""
        if self._connections.settings.environment == "production":
            raise RuntimeError("automatic production schema initialization is disabled")
        async with self._connections.connect() as connection:
            await connection.executescript(SCHEMA_SQL)
            await connection.execute("DELETE FROM alembic_version")
            await connection.execute(
                "INSERT INTO alembic_version(version_num) VALUES (?)", (SCHEMA_REVISION,)
            )
            await connection.commit()

    async def persist(
        self,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
    ) -> IngressReceipt:
        return await self._ingress.persist(
            provider_id=provider_id,
            delivery_identity=delivery_identity,
            event_name=event_name,
            payload_sha256=payload_sha256,
            normalization=normalization,
            received_at=received_at,
        )

    async def lease_next(self, worker_id: str, now: datetime) -> JobLease | None:
        return await self._queue.lease_next(worker_id, now)

    async def heartbeat(self, lease: JobLease, now: datetime) -> bool:
        return await self._queue.heartbeat(lease, now)

    async def complete(
        self, lease: JobLease, *, head_sha: str, base_sha: str, now: datetime
    ) -> bool:
        return await self._queue.complete(lease, head_sha=head_sha, base_sha=base_sha, now=now)

    async def terminate(self, lease: JobLease, state: str, reason: str, now: datetime) -> bool:
        return await self._queue.terminate(lease, state, reason, now)

    async def retry(
        self,
        lease: JobLease,
        *,
        available_at: datetime,
        error_class: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        return await self._queue.retry(
            lease,
            available_at=available_at,
            error_class=error_class,
            error_message=error_message,
            now=now,
        )

    async def recover_expired(self, now: datetime) -> int:
        return await self._queue.recover_expired(now)

    async def installation_state(
        self, provider_id: str, installation_id: str
    ) -> InstallationState | None:
        return await self._installations.get(provider_id, installation_id)

    async def get_job(self, job_id: str) -> QueueJob | None:
        return await self._queue.get_job(job_id)

    async def check_ready(self) -> bool:
        return await self._readiness.check_ready()

    async def status(self) -> dict[str, int | float]:
        return await self._readiness.status()

    async def retain_terminal_history(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
        dry_run: bool,
        correlation_id: str,
    ) -> RetentionResult:
        return await self._retention.retain_terminal_history(
            cutoff=cutoff,
            batch_size=batch_size,
            dry_run=dry_run,
            correlation_id=correlation_id,
        )
