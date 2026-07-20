"""Worker command and composition."""

import argparse
import asyncio
import signal
import socket

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.persistence.sqlite.locks import MaintenanceLock
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import compose_github
from revio.adapters.scm.github.credentials import GitHubCredentialCache
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.registries import ProviderRegistry


async def _run(check_only: bool) -> int:
    database = DatabaseSettings()
    queue = QueueSettings()
    store = SQLiteStore(database, queue)
    lock = MaintenanceLock(
        database.database_path.with_name("revio.maintenance.lock"), exclusive=False
    )
    with lock:
        if not await store.check_ready():
            return 1
        if check_only:
            return 0
        providers = ProviderRegistry()
        github_settings = GitHubSettings()
        if not github_settings.github_enabled:
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stopped.set)
            await stopped.wait()
            return 0
        github = compose_github(github_settings)
        providers.register_scm(GITHUB_PROVIDER_ID, github.bundle)
        credential_cache = GitHubCredentialCache(github.token_cache)
        processor = QueueProcessor(store, providers, credential_cache, queue)
        worker = QueueWorker(store, processor, queue, f"{socket.gethostname()}:{id(store)}")
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, worker.request_stop)
        try:
            await worker.run()
        finally:
            await github.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="revio-worker")
    parser.add_argument("command", choices=("run", "check-ready"), nargs="?", default="run")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.command == "check-ready")))
