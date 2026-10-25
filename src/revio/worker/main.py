"""Worker command and composition."""

import argparse
import asyncio
import signal
import socket
import sys
from typing import cast

from revio.adapters.ai.anthropic import (
    ANTHROPIC_PROVIDER_ID,
    AnthropicReviewAdapter,
    anthropic_agent_model_profile,
    anthropic_model_profile,
)
from revio.adapters.ai.openai import OPENAI_PROVIDER_ID, OpenAIReviewAdapter, openai_model_profile
from revio.adapters.observability import configure_logging
from revio.adapters.observability.prometheus import PrometheusQueueMetrics
from revio.adapters.observability.server import serve_metrics
from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.persistence.sqlite.locks import MaintenanceLock, WorkerInstanceLock
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import compose_github
from revio.adapters.scm.github.credentials import GitHubCredentialCache
from revio.application.queue.processor import QueueProcessor
from revio.application.queue.service import QueueWorker
from revio.application.repository_configuration import RepositoryConfigurationExecutor
from revio.application.review.agent import AgentReviewService
from revio.application.review.agent_tools import AgentLimits
from revio.application.review.execution import PreflightedReviewer, ReviewGenerationService
from revio.application.review.job import ReviewJobExecutor
from revio.application.review.lifecycle import FindingLifecycleService
from revio.application.review.markers import load_marker_key, marker_key_id
from revio.config.anthropic import AnthropicSettings
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings
from revio.config.observability import ObservabilitySettings
from revio.config.openai import OpenAISettings
from revio.config.queue import QueueSettings
from revio.config.review import ReviewSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ModelAlias, ProviderId
from revio.registries import ModelRegistry, ProviderRegistry, SCMAdapterBundle

# Concrete review adapters known to the composition root. Both satisfy the provider-neutral
# PreflightedReviewer and AIFindingRevalidator surfaces consumed by the application layer; adding a
# provider means adding its adapter here and one enablement line in run_worker.
AIReviewAdapter = AnthropicReviewAdapter | OpenAIReviewAdapter


def resolve_review_profile(settings: ReviewSettings) -> ResolvedModelProfile:
    models = ModelRegistry()
    models.register(anthropic_model_profile())
    models.register(anthropic_agent_model_profile())
    models.register(openai_model_profile())
    alias = (
        settings.review_model_alias
        if settings.review_model_alias is not None
        else settings.review_fallback_alias
    )
    profile = models.resolve(ModelAlias(value=alias))
    if "diff_only" not in profile.allowed_review_modes:
        raise ValueError("resolved review model does not support diff_only")
    return profile


def select_reviewer(
    ai_adapter: AIReviewAdapter | None,
    profile: ResolvedModelProfile | None,
    settings: ReviewSettings,
    bundle: SCMAdapterBundle | None,
) -> PreflightedReviewer | None:
    """Pick the agent reviewer when every gate is satisfied; otherwise stay diff-only.

    Agent mode is triple-gated: the administrator switch, an agent-capable model profile (declaring
    the ``agent`` mode and native tool use), and an SCM that advertises file and tree access. Any
    missing gate falls back to the diff-only reviewer with byte-identical behavior.
    """
    if ai_adapter is None or profile is None:
        return ai_adapter
    if (
        settings.review_agent_mode_enabled
        and "agent" in profile.allowed_review_modes
        and profile.native_tools
        and isinstance(ai_adapter, AnthropicReviewAdapter)
        and bundle is not None
        and bundle.repository_content is not None
        and bundle.capabilities.repository_file_access
        and bundle.capabilities.tree_access
    ):
        return AgentReviewService(
            ai_adapter,
            bundle.repository_content,
            limits=AgentLimits(
                max_tool_calls=settings.review_agent_max_tool_calls,
                max_files=settings.review_agent_max_files,
                max_bytes_per_file=settings.review_agent_max_bytes_per_file,
                max_aggregate_bytes=settings.review_agent_max_aggregate_bytes,
                max_tree_entries=settings.review_agent_max_tree_entries,
            ),
            max_turns=settings.review_agent_max_turns,
        )
    return ai_adapter


async def run_worker(check_only: bool, *, health_only: bool = False) -> int:
    observability = ObservabilitySettings()
    configure_logging(observability)
    database = DatabaseSettings()
    queue = QueueSettings()
    metrics = PrometheusQueueMetrics() if observability.metrics_enabled else None
    store = SQLiteStore(database, queue, metrics=metrics)
    github_settings = GitHubSettings()
    review_settings = ReviewSettings()
    anthropic_settings = AnthropicSettings()
    openai_settings = OpenAISettings()
    github = (
        compose_github(github_settings, review_settings=review_settings)
        if github_settings.github_enabled
        else None
    )
    # Composition root: construct every enabled AI adapter keyed by its provider id. Selection is
    # purely by the resolved model profile's provider_id below; no other layer branches on provider.
    ai_adapters: dict[ProviderId, AIReviewAdapter] = {}
    if anthropic_settings.anthropic_enabled:
        ai_adapters[ANTHROPIC_PROVIDER_ID] = AnthropicReviewAdapter(
            anthropic_settings, review_settings
        )
    if openai_settings.openai_enabled:
        ai_adapters[OPENAI_PROVIDER_ID] = OpenAIReviewAdapter(openai_settings, review_settings)
    review_profile = (
        resolve_review_profile(review_settings) if review_settings.review_enabled else None
    )
    ai_adapter: AIReviewAdapter | None = None
    if review_profile is not None:
        ai_adapter = ai_adapters.get(review_profile.provider_id)
        if ai_adapter is None:
            raise ValueError("selected review provider is not enabled")
    reviewer = select_reviewer(
        ai_adapter, review_profile, review_settings, github.bundle if github is not None else None
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
                if ai_adapter is not None and review_profile is None:
                    raise RuntimeError("review model profile was not resolved")
                lifecycle = (
                    FindingLifecycleService(
                        store.findings,
                        github.adapter,
                        github.adapter,
                        revalidator=ai_adapter,
                        thread_reader=github.thread_reader,
                        thread_resolver=github.thread_resolver,
                        marker_key=active_marker_key,
                    )
                    if (
                        ai_adapter is not None
                        and review_settings.review_publish_enabled
                        and active_marker_key is not None
                        and github.thread_reader is not None
                        and github.thread_resolver is not None
                    )
                    else None
                )
                processor = QueueProcessor(
                    store,
                    providers,
                    credential_cache,
                    queue,
                    metrics=store.metrics,
                    review_executor=(
                        RepositoryConfigurationExecutor(
                            store.configurations,
                            github.adapter,
                            ReviewJobExecutor(
                                store.reviews,
                                github.adapter,
                                ReviewGenerationService(
                                    store.reviews,
                                    cast(PreflightedReviewer, reviewer),
                                    review_settings,
                                ),
                                cast(ResolvedModelProfile, review_profile),
                                review_settings,
                                writer=github.writer,
                                marker_key=active_marker_key,
                                configuration_repository=store.configurations,
                                lifecycle=lifecycle,
                            ),
                            review_settings,
                        )
                        if ai_adapter is not None
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
                if metrics is not None:
                    async with serve_metrics(
                        metrics, bind=observability.metrics_bind, port=observability.metrics_port
                    ):
                        await worker.run()
                else:
                    await worker.run()
        return 0
    finally:
        if github is not None:
            await github.close()
        for adapter in ai_adapters.values():
            await adapter.close()


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
