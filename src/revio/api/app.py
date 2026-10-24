"""FastAPI application construction and infrastructure composition."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from revio.adapters.persistence.sqlite import SQLiteStore
from revio.adapters.persistence.sqlite.locks import MaintenanceLock
from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import GitHubComposition, compose_github
from revio.adapters.scm.github.webhook.ingress import GitHubSandboxWebhookIngress
from revio.adapters.scm.github.webhook.route import router as github_webhook_router
from revio.api.routes.health import router as health_router
from revio.api.routes.readiness import router as readiness_router
from revio.application.review.markers import load_marker_key, marker_key_id
from revio.config.database import DatabaseSettings
from revio.config.github import GitHubSettings
from revio.config.queue import QueueSettings
from revio.config.review import ReviewSettings
from revio.registries import ProviderRegistry


def create_app(
    settings: GitHubSettings | None = None,
    *,
    database_settings: DatabaseSettings | None = None,
    queue_settings: QueueSettings | None = None,
    persistence: SQLiteStore | None = None,
    review_settings: ReviewSettings | None = None,
) -> FastAPI:
    """Build and configure the Revio ASGI application."""
    github_settings = settings or GitHubSettings()
    review_policy = review_settings or ReviewSettings()
    registry = ProviderRegistry()
    github: GitHubComposition | None = None
    if github_settings.github_enabled:
        github = compose_github(github_settings, review_settings=review_policy)
        registry.register_scm(GITHUB_PROVIDER_ID, github.bundle)
    durable = persistence
    if github_settings.github_webhook_mode == "durable" and durable is None:
        durable = SQLiteStore(database_settings or DatabaseSettings(), queue_settings)
    runtime_lock: MaintenanceLock | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        nonlocal runtime_lock
        if durable is not None:
            runtime_lock = MaintenanceLock(
                durable.path.with_name("revio.maintenance.lock"), exclusive=False
            ).acquire()
            if not await durable.check_ready():
                runtime_lock.release()
                runtime_lock = None
                raise RuntimeError("durable persistence is not ready")
            if review_policy.review_publish_enabled:
                active_key_id = marker_key_id(load_marker_key(review_policy))
                unresolved = await durable.reviews.unresolved_marker_key_ids()
                if unresolved - {active_key_id}:
                    runtime_lock.release()
                    runtime_lock = None
                    raise RuntimeError("unresolved publication uses another marker key")
        try:
            yield
        finally:
            if runtime_lock is not None:
                runtime_lock.release()
            if github is not None:
                await github.close()

    application = FastAPI(
        title="Revio",
        description="Provider-neutral AI code review",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.include_router(readiness_router)
    application.state.github_settings = github_settings
    application.state.review_settings = review_policy
    application.state.provider_registry = registry
    application.state.github_composition = github
    application.state.github_webhook_ingress = (
        GitHubSandboxWebhookIngress(github.token_cache) if github is not None else None
    )
    application.state.durable_ingress = durable
    application.state.persistence = durable
    application.include_router(github_webhook_router)
    return application
