"""Application construction tests."""

from fastapi import FastAPI

from revio.api.app import create_app


def test_create_app_returns_fastapi_application() -> None:
    application = create_app()

    assert isinstance(application, FastAPI)
    assert application.title == "Revio"
