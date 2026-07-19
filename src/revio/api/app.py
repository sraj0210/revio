"""FastAPI application construction."""

from fastapi import FastAPI

from revio.api.routes.health import router as health_router


def create_app() -> FastAPI:
    """Build and configure the Revio ASGI application."""
    application = FastAPI(
        title="Revio",
        description="Provider-neutral AI code review",
        version="0.1.0",
    )
    application.include_router(health_router)
    return application
