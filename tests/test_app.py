"""Application construction tests."""

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from revio.adapters.scm.github import GITHUB_PROVIDER_ID
from revio.adapters.scm.github.errors import GitHubConfigurationError
from revio.api.app import create_app
from revio.config.github import GitHubSettings
from revio.errors import UnknownProviderError


def test_create_app_returns_fastapi_application() -> None:
    application = create_app()

    assert isinstance(application, FastAPI)
    assert application.title == "Revio"


def test_disabled_github_is_not_composed() -> None:
    application = create_app(GitHubSettings(github_enabled=False))
    assert application.state.github_composition is None
    with pytest.raises(UnknownProviderError):
        application.state.provider_registry.scm(GITHUB_PROVIDER_ID)


def test_enabled_github_is_validated_and_registered(rsa_private_key_pem: str) -> None:
    application = create_app(
        GitHubSettings(
            github_enabled=True,
            github_app_id=1,
            github_private_key=SecretStr(rsa_private_key_pem),
        )
    )
    bundle = application.state.provider_registry.scm(GITHUB_PROVIDER_ID)
    assert bundle.repository_content is not None
    assert bundle.publisher is None


def test_invalid_enabled_key_fails_application_construction() -> None:
    settings = GitHubSettings(
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr("not-a-key"),
    )
    with pytest.raises(GitHubConfigurationError):
        create_app(settings)


def test_api_uses_same_production_publishing_policy_as_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIO_ENVIRONMENT", "production")
    monkeypatch.setenv("REVIO_REVIEW_ENABLED", "true")
    monkeypatch.setenv("REVIO_REVIEW_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("REVIO_PUBLISH_MARKER_KEY", "x" * 32)
    with pytest.raises(Exception, match="forbidden in production"):
        create_app()
