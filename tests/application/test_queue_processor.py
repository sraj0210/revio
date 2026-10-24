"""Worker lifecycle gating and current-head-only behavior."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.errors import GitHubAuthenticationError, GitHubTransportError
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.domain.capabilities import SCMCapabilities
from revio.domain.identifiers import ChangeRequestTarget, InstallationRef
from revio.domain.models import ChangeRequest, DiffCollection
from revio.domain.queue import JobLease
from revio.errors import (
    LeaseLostError,
    PersistenceUnavailableError,
    ProviderCallTerminalError,
    ProviderWriteRejectedError,
)
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


class FailingReviewExecutor:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def execute(
        self, lease: JobLease, current: ChangeRequest, *, now: datetime | None = None
    ) -> str:
        raise self.error


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


@pytest.mark.asyncio
async def test_lease_lost_before_provider_call_stops_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    processor, reader, _ = _processor(store)

    async def lost(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(store, "heartbeat", lost)
    with pytest.raises(LeaseLostError):
        await processor.process(lease)
    assert reader.reads == reader.diff_reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["complete", "retry", "terminate"])
async def test_transition_cas_false_is_lease_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    processor, reader, _ = _processor(store)

    async def unchanged(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(store, transition, unchanged)
    if transition == "retry":

        async def transient(target: ChangeRequestTarget) -> ChangeRequest:
            raise GitHubTransportError("safe transient failure")

        monkeypatch.setattr(reader, "get_change_request", transient)
    elif transition == "terminate":

        async def closed(target: ChangeRequestTarget) -> ChangeRequest:
            return ChangeRequest(
                target=target,
                title="title",
                base_sha="base",
                head_sha="head",
                state="closed",
            )

        monkeypatch.setattr(reader, "get_change_request", closed)
    with pytest.raises(LeaseLostError):
        await processor.process(lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence_failure", [False, True])
async def test_heartbeat_loss_cancels_active_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistence_failure: bool
) -> None:
    queue = QueueSettings(queue_lease_seconds=10, queue_heartbeat_seconds=1)
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
    original_heartbeat = store.heartbeat
    calls = 0

    async def heartbeat(lease: JobLease, now: datetime) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original_heartbeat(lease, now)
        if persistence_failure:
            raise PersistenceUnavailableError("database is temporarily unavailable")
        return False

    monkeypatch.setattr(store, "heartbeat", heartbeat)
    processing = asyncio.create_task(worker.process_one())
    await reader.started.wait()
    if persistence_failure:
        with pytest.raises(PersistenceUnavailableError):
            await processing
    else:
        assert await processing
    assert len(reader.reads) == 1
    assert (await store.status())["running"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_state"),
    [
        ("transient", "retry_wait"),
        ("authentication", "dead"),
        ("closed", "cancelled"),
        ("stale", "superseded"),
    ],
)
async def test_provider_failure_and_current_head_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_state: str,
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    processor, reader, _ = _processor(store)

    async def result(target: ChangeRequestTarget) -> ChangeRequest:
        if outcome == "transient":
            raise GitHubTransportError("safe transient failure")
        if outcome == "authentication":
            raise GitHubAuthenticationError("safe authentication failure")
        return ChangeRequest(
            target=target,
            title="title",
            base_sha="base",
            head_sha="different" if outcome == "stale" else "head",
            state="closed" if outcome == "closed" else "open",
        )

    monkeypatch.setattr(reader, "get_change_request", result)
    await processor.process(lease)
    assert (await store.status())[expected_state] == 1


@pytest.mark.asyncio
async def test_retry_exhaustion_uses_total_lease_count(tmp_path: Path) -> None:
    queue = QueueSettings(queue_max_attempts=1)
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"), queue)
    await store.initialize()
    await _job(store)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None and lease.attempt_number == 1
    processor, reader, _ = _processor(store)

    async def transient(target: ChangeRequestTarget) -> ChangeRequest:
        raise GitHubTransportError("safe transient failure")

    reader.get_change_request = transient
    await processor.process(lease)
    assert (await store.status())["dead"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ProviderCallTerminalError("refusal"), ProviderWriteRejectedError("GitHub rejection")],
)
async def test_expected_review_provider_failures_never_escape_worker_processing(
    tmp_path: Path, error: Exception
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    lease = await store.lease_next("worker", datetime.now(UTC))
    assert lease is not None
    _, reader, cache = _processor(store)
    providers = ProviderRegistry()
    providers.register_scm(
        GITHUB_PROVIDER_ID,
        SCMAdapterBundle(reader=reader, capabilities=SCMCapabilities()),
    )
    processor = QueueProcessor(
        store,
        providers,
        cache,
        QueueSettings(),
        review_executor=FailingReviewExecutor(error),
    )
    await processor.process(lease)
    assert reader.reads and (await store.status())["dead"] == 1


@pytest.mark.asyncio
async def test_worker_processes_valid_job_after_malformed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(DatabaseSettings(database_path=tmp_path / "revio.db"))
    await store.initialize()
    await _job(store)
    second = normalize_webhook(
        "pull_request",
        "second",
        {
            "action": "opened",
            "installation": {"id": 9},
            "repository": {"id": 10, "name": "repo", "full_name": "owner/repo"},
            "pull_request": {
                "number": 3,
                "base": {"sha": "base"},
                "head": {"sha": "second"},
            },
        },
    )
    await store.persist(
        provider_id="github",
        delivery_identity="github:second",
        event_name="pull_request",
        payload_sha256="b" * 64,
        normalization=second,
        received_at=datetime.now(UTC),
    )
    async with store.connect() as connection:
        await connection.execute(
            "UPDATE queue_jobs SET event_json='{' WHERE semantic_identity LIKE '%:head:head:review'"
        )
        await connection.commit()
    processor, reader, _ = _processor(store)

    async def current(target: ChangeRequestTarget) -> ChangeRequest:
        return ChangeRequest(
            target=target,
            title="title",
            base_sha="base",
            head_sha="second",
            state="open",
        )

    monkeypatch.setattr(reader, "get_change_request", current)
    worker = QueueWorker(store, processor, QueueSettings(), "worker")
    assert await worker.process_one()
    status = await store.status()
    assert status["dead"] == status["completed"] == 1
