"""Administrator-controlled OpenAI configuration.

Mirrors ``AnthropicSettings`` field-for-field in security posture: exactly one key source, a
production file-source requirement, an absolute key-file path, and a locked base URL. Azure OpenAI
and arbitrary OpenAI-compatible endpoints are out of scope and rejected here.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenAISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    environment: Literal["local", "sandbox", "production"] = "local"
    openai_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_api_key_file: Path | None = None
    openai_api_url: str = "https://api.openai.com"
    openai_http_timeout_seconds: float = Field(default=60, gt=0, le=300)

    @model_validator(mode="after")
    def validate_openai(self) -> Self:
        sources = sum(x is not None for x in (self.openai_api_key, self.openai_api_key_file))
        if self.openai_enabled and sources != 1:
            raise ValueError("exactly one OpenAI API-key source is required")
        if (
            self.openai_enabled
            and self.environment == "production"
            and self.openai_api_key_file is None
        ):
            raise ValueError("production requires an OpenAI API-key file source")
        if self.openai_api_key_file is not None and not self.openai_api_key_file.is_absolute():
            raise ValueError("OpenAI API-key file must be absolute")
        if self.openai_api_url != "https://api.openai.com":
            raise ValueError("Phase 8 supports only https://api.openai.com")
        return self
