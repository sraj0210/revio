"""Worker lifecycle gating and current-head-only behavior."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.capabilities import SCMCapabilities
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef
from revio.domain.models import ChangeRequest, DiffCollection
from revio.registries import ProviderRegistry, SCMAdapterBundle


@dataclass
class RecordingReader:
    reads: list[ChangeRequestTarget] = field(default_factory=lambda: list[ChangeRequestTarget]())
    diff_reads: list[ChangeRequestTarget] = field(
        default_factory=lambda: list[ChangeRequestTarget]()
    )

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        self.reads.append(target)
        return ChangeRequest(
            target=target,
            title="title",
            base_sha="base",
            head_sha="head",
            state="open",
        )

    async def get_diff(self, target: ChangeRequestTarget) -> DiffCollection:
        self.diff_reads.append(target)
        return DiffCollection()


@dataclass
class RecordingCache:
    invalidated: list[InstallationRef] = field(default_factory=lambda: list[InstallationRef]())

    async def invalidate(self, installation: InstallationRef) -> None:
        self.invalidated.append(installation)


class BlockingReader(RecordingReader):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_change_request(self, target: ChangeRequestTarget) -> ChangeRequest:
        self.reads.append(target)
        self.started.set()
        await self.release.wait()
        return ChangeRequest(
            target=target,
            title="title",
            base_sha="base",
            head_sha="head",
            state="open",
        )


async def _job(store: SQLiteStore) -> None:
    normalization = normalize_webhook(
        "pull_request",
        "pull",
        {
            "action": "opened",
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {
                "number": 3,
                "base": {"sha": "base"},
                "head": {"sha": "head"},
            },
        },
    )
    await store.persist(
        provider_id="github",
        delivery_identity="github:pull",
        event_name="pull_request",
        payload_sha256="a" * 64,
        normalization=normalization,
        received_at=datetime.now(UTC),
    )


async def _lifecycle(store: SQLiteStore, delivery: str, action: str, updated_at: datetime) -> None:
    normalization = normalize_webhook(
        "installation",
        delivery,
        {"action": action, "installation": {"id": 9, "updated_at": updated_at.isoformat()}},
    )
    await store.persist(
        provider_id="github",
        delivery_identity=f"github:{delivery}",
        event_name="installation",
        payload_sha256=(delivery + "0" * 64)[:64],
        normalization=normalization,
        received_at=datetime.now(UTC),
    )


def _processor(store: SQLiteStore) -> tuple[QueueProcessor, RecordingReader, RecordingCache]:
    reader = RecordingReader()
    cache = RecordingCache()
    providers = ProviderRegistry()
    providers.register_scm(
        GITHUB_PROVIDER_ID,
        SCMAdapterBundle(reader=reader, capabilities=SCMCapabilities()),
    )
    return QueueProcessor(store, providers, cache, QueueSettings()), reader, cache


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["suspend", "deleted"])
async def test_lifecycle_change_after_enqueue_blocks_all_provider_reads(
    tmp_path: Path, action: str
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    await _lifecycle(store, "lifecycle", action, datetime.now(UTC))
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    processor, reader, cache = _processor(store)
    await processor.process(lease)
    result = await store.get_job(lease.job.id)
    assert result is not None and result.state == "cancelled" and result.terminal_at is not None
    assert reader.reads == reader.diff_reads == []
    assert cache.invalidated == [lease.job.event.installation]


@pytest.mark.asyncio
async def test_reactivation_allows_only_current_change_request_read(tmp_path: Path) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    now = datetime.now(UTC)
    await _lifecycle(store, "suspend", "suspend", now)
    await _lifecycle(store, "activate", "unsuspend", now + timedelta(seconds=1))
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    processor, reader, cache = _processor(store)
    await processor.process(lease)
    result = await store.get_job(lease.job.id)
    assert result is not None and result.state == "completed"
    assert len(reader.reads) == 1 and reader.diff_reads == []
    assert cache.invalidated == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_during_shutdown", [True, False])
async def test_graceful_shutdown_completes_or_leaves_lease_for_expiry(
    tmp_path: Path, finish_during_shutdown: bool
) -> None:
    queue = QueueSettings(
        queue_lease_seconds=10,
        queue_heartbeat_seconds=4,
        queue_shutdown_timeout_seconds=0.05,
    )
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"), queue)
    await store.initialize()
    await _job(store)
    reader = BlockingReader()
    providers = ProviderRegistry()
    providers.register_scm(
        GITHUB_PROVIDER_ID,
        SCMAdapterBundle(reader=reader, capabilities=SCMCapabilities()),
    )
    processor = QueueProcessor(store, providers, RecordingCache(), queue)
    worker = QueueWorker(store, processor, queue, "worker")
    running = asyncio.create_task(worker.process_one())
    await reader.started.wait()
    worker.request_stop()
    if finish_during_shutdown:
        reader.release.set()
    assert await running
    status = await store.status()
    if finish_during_shutdown:
        assert status["completed"] == 1
    else:
        assert status["running"] == 1
