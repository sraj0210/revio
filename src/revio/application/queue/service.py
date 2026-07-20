"""Single-worker polling, heartbeat, and graceful-shutdown service."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

from revio.application.queue.processor import QueueProcessor
from revio.config.queue import QueueSettings
from revio.domain.queue import JobLease
from revio.errors import PersistenceUnavailableError
from revio.ports.persistence import QueueRepository


class QueueWorker:
    def __init__(
        self,
        repository: QueueRepository,
        processor: QueueProcessor,
        settings: QueueSettings,
        worker_id: str,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._settings = settings
        self._worker_id = worker_id
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def _heartbeat(self, lease: JobLease, done: asyncio.Event) -> None:
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=self._settings.queue_heartbeat_seconds)
            except TimeoutError:
                if not await self._repository.heartbeat(lease, datetime.now(UTC)):
                    return

    async def process_one(self) -> bool:
        now = datetime.now(UTC)
        await self._repository.recover_expired(now)
        lease = await self._repository.lease_next(self._worker_id, now)
        if lease is None:
            return False
        done = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(lease, done))
        work = asyncio.create_task(self._processor.process(lease))
        stopping = asyncio.create_task(self._stop.wait())
        try:
            finished, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if work in finished:
                await work
            else:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(work),
                        timeout=self._settings.queue_shutdown_timeout_seconds,
                    )
                except TimeoutError:
                    work.cancel()
                    with suppress(asyncio.CancelledError):
                        await work
        finally:
            stopping.cancel()
            with suppress(asyncio.CancelledError):
                await stopping
            done.set()
            await heartbeat
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
