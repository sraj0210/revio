"""Administrator-controlled Anthropic configuration."""

from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnthropicSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    anthropic_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    anthropic_api_key_file: Path | None = None
    anthropic_api_url: str = "https://api.anthropic.com"
    anthropic_api_version: str = "2023-06-01"
    anthropic_http_timeout_seconds: float = Field(default=60, gt=0, le=300)

    @model_validator(mode="after")
    def validate_anthropic(self) -> Self:
        sources = sum(x is not None for x in (self.anthropic_api_key, self.anthropic_api_key_file))
        if self.anthropic_enabled and sources != 1:
            raise ValueError("exactly one Anthropic API-key source is required")
        if (
            self.anthropic_api_key_file is not None
            and not self.anthropic_api_key_file.is_absolute()
        ):
            raise ValueError("Anthropic API-key file must be absolute")
        if self.anthropic_api_url != "https://api.anthropic.com":
            raise ValueError("Phase 4 supports only https://api.anthropic.com")
        return self
