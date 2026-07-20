"""Worker command and composition."""

import argparse
import asyncio
import signal
import socket
import sys

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.persistence.sqlite.locks import MaintenanceLock, WorkerInstanceLock
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import compose_github
from revio.adapters.scm.github.credentials import GitHubCredentialCache
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.registries import ProviderRegistry


async def run_worker(check_only: bool, *, health_only: bool = False) -> int:
    database = DatabaseSettings()
    queue = QueueSettings()
    store = SQLiteStore(database, queue)
    github_settings = GitHubSettings()
    github = compose_github(github_settings) if github_settings.github_enabled else None
    lock = MaintenanceLock(
        database.database_path.with_name("revio.maintenance.lock"), exclusive=False
    )
    worker_lock = WorkerInstanceLock(database.database_path.with_name("revio.worker.lock"))
    try:
        with lock:
            if not await store.check_ready():
                return 1
            if health_only:
                return 0
            with worker_lock:
                if check_only:
                    return 0
                if github is None:
                    stopped = asyncio.Event()
                    loop = asyncio.get_running_loop()
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        loop.add_signal_handler(signum, stopped.set)
                    await stopped.wait()
                    return 0
                providers = ProviderRegistry()
                providers.register_scm(GITHUB_PROVIDER_ID, github.bundle)
                credential_cache = GitHubCredentialCache(github.token_cache)
                processor = QueueProcessor(
                    store, providers, credential_cache, queue, metrics=store.metrics
                )
                worker = QueueWorker(
                    store,
                    processor,
                    queue,
                    f"{socket.gethostname()}:{id(store)}",
                    metrics=store.metrics,
                )
                loop = asyncio.get_running_loop()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(signum, worker.request_stop)
                await worker.run()
        return 0
    finally:
        if github is not None:
            await github.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="revio-worker")
    parser.add_argument(
        "command", choices=("run", "check-ready", "health"), nargs="?", default="run"
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(
            run_worker(args.command == "check-ready", health_only=args.command == "health")
        )
    except Exception:
        print("worker is not ready", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(result)
