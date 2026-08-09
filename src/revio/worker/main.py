"""Worker command and composition."""

import argparse
import asyncio
import signal
import socket
import sys
from typing import cast

from revio.adapters.ai.anthropic import AnthropicReviewAdapter, anthropic_model_profile
from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.persistence.sqlite.locks import MaintenanceLock, WorkerInstanceLock
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import compose_github
from revio.adapters.scm.github.credentials import GitHubCredentialCache
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.application.review.execution import ReviewGenerationService
from revio.application.review.job import ReviewJobExecutor
from revio.application.review.markers import load_marker_key, marker_key_id
from revio.config.anthropic import AnthropicSettings
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.config.review import ReviewSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ModelAlias
from revio.registries import ModelRegistry, ProviderRegistry


def resolve_review_profile(settings: ReviewSettings):
    models = ModelRegistry()
    models.register(anthropic_model_profile())
    alias = settings.review_model_alias or settings.review_fallback_alias
    profile = models.resolve(ModelAlias(value=alias))
    if "diff_only" not in profile.allowed_review_modes:
        raise ValueError("resolved review model does not support diff_only")
    return profile


async def run_worker(check_only: bool, *, health_only: bool = False) -> int:
    database = DatabaseSettings()
    queue = QueueSettings()
    store = SQLiteStore(database, queue)
    github_settings = GitHubSettings()
    review_settings = ReviewSettings()
    anthropic_settings = AnthropicSettings()
    if review_settings.review_enabled and not anthropic_settings.anthropic_enabled:
        raise ValueError("review execution requires Anthropic")
    github = (
        compose_github(github_settings, review_settings=review_settings)
        if github_settings.github_enabled
        else None
    )
    anthropic = (
        AnthropicReviewAdapter(anthropic_settings, review_settings)
        if review_settings.review_enabled
        else None
    )
    review_profile = (
        resolve_review_profile(review_settings) if review_settings.review_enabled else None
    )
    if github is None and not github_settings.github_allow_idle_worker:
        raise ValueError("worker requires an enabled provider adapter")
    lock = MaintenanceLock(
        database.database_path.with_name("revio.maintenance.lock"), exclusive=False
    )
    worker_lock = WorkerInstanceLock(database.database_path.with_name("revio.worker.lock"))
    try:
        with lock:
            if not await store.check_ready():
                return 1
            active_marker_key: bytes | None = None
            if review_settings.review_publish_enabled:
                active_marker_key = load_marker_key(review_settings)
                unresolved = await store.reviews.unresolved_marker_key_ids()
                if unresolved - {marker_key_id(active_marker_key)}:
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
                if anthropic is not None and review_profile is None:
                    raise RuntimeError("review model profile was not resolved")
                processor = QueueProcessor(
                    store,
                    providers,
                    credential_cache,
                    queue,
                    metrics=store.metrics,
                    review_executor=(
                        ReviewJobExecutor(
                            store.reviews,
                            github.adapter,
                            ReviewGenerationService(store.reviews, anthropic, review_settings),
                            cast(ResolvedModelProfile, review_profile),
                            review_settings,
                            writer=github.writer,
                            marker_key=active_marker_key,
                        )
                        if anthropic is not None
                        else None
                    ),
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
        if anthropic is not None:
            await anthropic.close()


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
