"""Conditional GitHub settings tests."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from revio.config.github import GitHubSettings


def test_disabled_github_requires_no_credentials() -> None:
    settings = GitHubSettings(github_enabled=False)
    assert settings.github_app_id is None


def test_enabled_github_requires_app_and_one_key(rsa_private_key_pem: str) -> None:
    settings = GitHubSettings(
        github_enabled=True, github_app_id=1, github_private_key=SecretStr(rsa_private_key_pem)
    )
    assert settings.github_app_id == 1
    with pytest.raises(ValidationError):
        GitHubSettings(github_enabled=True)
    with pytest.raises(ValidationError):
        GitHubSettings(
            github_enabled=True,
            github_app_id=1,
            github_private_key=SecretStr(rsa_private_key_pem),
            github_private_key_file=Path("/secret.pem"),
        )


def test_webhook_secret_is_conditional_and_production_is_blocked(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(
            github_enabled=True,
            github_app_id=1,
            github_private_key=SecretStr(rsa_private_key_pem),
            github_sandbox_webhook_enabled=True,
        )
    with pytest.raises(ValidationError):
        GitHubSettings(
            github_enabled=True,
            github_app_id=1,
            github_private_key=SecretStr(rsa_private_key_pem),
            github_sandbox_webhook_enabled=True,
            github_webhook_secret=SecretStr("secret"),
            environment="production",
        )
    assert GitHubSettings(
        github_enabled=True,
        github_app_id=1,
        github_private_key=SecretStr(rsa_private_key_pem),
        github_sandbox_webhook_enabled=True,
        github_webhook_secret=SecretStr("secret"),
        environment="sandbox",
    ).github_sandbox_webhook_enabled
