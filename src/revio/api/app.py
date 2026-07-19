"""FastAPI application construction."""

from datetime import timedelta

from fastapi import FastAPI

from revio.adapters.scm.github.auth import InstallationTokenCache
from revio.api.routes.github_webhook import router as github_webhook_router
from revio.api.routes.health import router as health_router
from revio.application.webhook.service import GitHubSandboxWebhookService
from revio.config.github import GitHubSettings


def create_app(settings: GitHubSettings | None = None) -> FastAPI:
    """Build and configure the Revio ASGI application."""
    application = FastAPI(
        title="Revio",
        description="Provider-neutral AI code review",
        version="0.1.0",
    )
    application.include_router(health_router)
    github_settings = settings or GitHubSettings()
    application.state.github_settings = github_settings
    cache = InstallationTokenCache(
        timedelta(seconds=github_settings.github_token_refresh_margin_seconds),
        timedelta(seconds=github_settings.github_token_minimum_usable_lifetime_seconds),
    )
    application.state.github_webhook_service = GitHubSandboxWebhookService(cache)
    application.include_router(github_webhook_router)
    return application
