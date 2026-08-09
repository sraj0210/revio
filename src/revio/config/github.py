"""GitHub settings with conditional validation."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GitHubSettings(BaseSettings):
    """Administrator-controlled GitHub configuration."""

    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    environment: Literal["local", "sandbox", "production"] = "local"
    github_enabled: bool = False
    github_allow_idle_worker: bool = False
    github_webhook_mode: Literal["disabled", "sandbox", "durable"] = "disabled"
    # Phase 2 compatibility input; normalized into github_webhook_mode.
    github_sandbox_webhook_enabled: bool = False
    github_app_id: int | None = Field(default=None, gt=0)
    github_private_key: SecretStr | None = None
    github_private_key_file: Path | None = None
    github_webhook_secret: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_api_version: str = "2022-11-28"
    github_http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    github_token_refresh_margin_seconds: int = Field(default=60, ge=0)
    github_token_minimum_usable_lifetime_seconds: int = Field(default=30, ge=0)
    github_webhook_max_bytes: int = Field(default=1_048_576, gt=0, le=25_000_000)
    github_max_pages: int = Field(default=30, gt=0, le=100)
    github_max_items: int = Field(default=3_000, gt=0, le=10_000)

    @model_validator(mode="after")
    def validate_github(self) -> Self:
        if self.github_sandbox_webhook_enabled and self.github_webhook_mode == "disabled":
            self.github_webhook_mode = "sandbox"
        key_sources = sum(
            source is not None for source in (self.github_private_key, self.github_private_key_file)
        )
        if self.github_allow_idle_worker and (
            self.environment != "local"
            or self.github_enabled
            or self.github_webhook_mode != "disabled"
        ):
            raise ValueError("idle worker mode is restricted to disabled local development")
        if self.github_enabled:
            if self.github_app_id is None:
                raise ValueError("GitHub App ID is required when GitHub is enabled")
            if key_sources != 1:
                raise ValueError("exactly one GitHub private-key source is required")
        if self.github_webhook_mode != "disabled":
            if not self.github_enabled:
                raise ValueError("webhook ingress requires the GitHub adapter")
            if self.github_webhook_secret is None:
                raise ValueError("webhook secret is required when webhook ingress is enabled")
            if self.github_webhook_mode == "sandbox" and self.environment == "production":
                raise ValueError("sandbox webhook cannot be enabled in production")
        if self.github_api_url != "https://api.github.com":
            raise ValueError("Phase 2 supports only https://api.github.com")
        return self
