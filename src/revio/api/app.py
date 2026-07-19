"""FastAPI application construction and infrastructure composition."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.composition import GitHubComposition, compose_github
from revio.adapters.scm.github.webhook.ingress import GitHubSandboxWebhookIngress
from revio.adapters.scm.github.webhook.route import router as github_webhook_router
from revio.api.routes.health import router as health_router
from revio.config.github import GitHubSettings
from revio.registries import ProviderRegistry


def create_app(settings: GitHubSettings | None = None) -> FastAPI:
    """Build and configure the Revio ASGI application."""
    github_settings = settings or GitHubSettings()
    registry = ProviderRegistry()
    github: GitHubComposition | None = None
    if github_settings.github_enabled:
        github = compose_github(github_settings)
        registry.register_scm(GITHUB_PROVIDER_ID, github.bundle)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            if github is not None:
                await github.close()

    application = FastAPI(
        title="Revio",
        description="Provider-neutral AI code review",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.state.github_settings = github_settings
    application.state.provider_registry = registry
    application.state.github_composition = github
    application.state.github_webhook_ingress = (
        GitHubSandboxWebhookIngress(github.token_cache) if github is not None else None
    )
    application.include_router(github_webhook_router)
    return application
