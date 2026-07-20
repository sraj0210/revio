"""Single-worker polling, heartbeat, and graceful-shutdown service."""

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime

from revio.application.queue.processor import QueueProcessor
from revio.config.queue import QueueSettings
from revio.domain.queue import JobLease
from revio.errors import LeaseLostError, PersistenceUnavailableError
from revio.ports.observability import QueueMetricsPort
from revio.ports.persistence import QueueRepository
from revio.ports.time import Clock

logger = logging.getLogger(__name__)


class QueueWorker:
    def __init__(
        self,
        repository: QueueRepository,
        processor: QueueProcessor,
        settings: QueueSettings,
        worker_id: str,
        metrics: QueueMetricsPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._settings = settings
        self._worker_id = worker_id
        self._metrics = metrics
        self._clock = clock
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(UTC)

    async def _heartbeat(
        self, lease: JobLease, done: asyncio.Event, work: asyncio.Task[None]
    ) -> bool:
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=self._settings.queue_heartbeat_seconds)
            except TimeoutError:
                try:
                    owned = await self._repository.heartbeat(lease, self._now())
                except PersistenceUnavailableError:
                    work.cancel()
                    raise
                if not owned:
                    work.cancel()
                    return False
        return True

    def _record_lease_lost(self) -> None:
        logger.warning("queue_job_lease_lost", extra={"correlation_id": uuid.uuid4().hex})
        if self._metrics is not None:
            self._metrics.increment("lease_lost")

    async def _finish_work(self, work: asyncio.Task[None]) -> bool:
        try:
            await work
        except LeaseLostError:
            self._record_lease_lost()
            return False
        return True

    async def process_one(self) -> bool:
        now = self._now()
        await self._repository.recover_expired(now)
        lease = await self._repository.lease_next(self._worker_id, now)
        if lease is None:
            return False
        done = asyncio.Event()
        work = asyncio.create_task(self._processor.process(lease))
        heartbeat = asyncio.create_task(self._heartbeat(lease, done, work))
        stopping = asyncio.create_task(self._stop.wait())
        heartbeat_handled = False
        try:
            finished, _ = await asyncio.wait(
                {work, stopping, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in finished:
                heartbeat_handled = True
                try:
                    heartbeat_owned = await heartbeat
                except PersistenceUnavailableError:
                    with suppress(asyncio.CancelledError):
                        await work
                    raise
                if not heartbeat_owned:
                    with suppress(asyncio.CancelledError):
                        await work
                    self._record_lease_lost()
                    return True
            if work in finished:
                await self._finish_work(work)
            else:
                graceful, _ = await asyncio.wait(
                    {work, heartbeat},
                    timeout=self._settings.queue_shutdown_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat in graceful:
                    heartbeat_handled = True
                    try:
                        heartbeat_owned = await heartbeat
                    except PersistenceUnavailableError:
                        with suppress(asyncio.CancelledError):
                            await work
                        raise
                    if not heartbeat_owned:
                        with suppress(asyncio.CancelledError):
                            await work
                        self._record_lease_lost()
                        return True
                if work in graceful:
                    await self._finish_work(work)
                else:
                    work.cancel()
                    with suppress(asyncio.CancelledError):
                        await work
        finally:
            stopping.cancel()
            with suppress(asyncio.CancelledError):
                await stopping
            done.set()
            if not heartbeat_handled:
                heartbeat_owned = await heartbeat
                if not heartbeat_owned:
                    self._record_lease_lost()
        return True

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.process_one()
            except PersistenceUnavailableError:
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._settings.queue_poll_interval_seconds
                    )
                except TimeoutError:
                    pass
