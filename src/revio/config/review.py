"""Phase 4 review execution and publication policy."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReviewSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    environment: Literal["local", "sandbox", "production"] = "local"
    review_enabled: bool = False
    review_publish_enabled: bool = False
    publish_marker_key: SecretStr | None = None
    publish_marker_key_file: Path | None = None
    review_model_alias: str = "review-default"
    review_fallback_alias: str = "review-default"
    review_input_token_ceiling: int = Field(default=120_000, gt=0, le=240_000)
    review_input_token_safety_margin: int = Field(default=2_000, gt=0, le=20_000)
    review_output_token_ceiling: int = Field(default=8_192, gt=0, le=16_384)
    review_max_files: int = Field(default=100, gt=0, le=300)
    review_max_lines: int = Field(default=12_000, gt=0, le=30_000)
    review_max_bytes: int = Field(default=524_288, gt=0, le=2_097_152)
    review_max_patch_bytes: int = Field(default=65_536, gt=0, le=262_144)
    review_max_findings: int = Field(default=50, gt=0, le=100)
    review_inline_confidence: float = Field(default=0.8, ge=0, le=1)
    review_summary_confidence: float = Field(default=0.6, ge=0, le=1)

    @property
    def admission_token_ceiling(self) -> int:
        return self.review_input_token_ceiling - self.review_input_token_safety_margin

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        if self.review_publish_enabled and not self.review_enabled:
            raise ValueError("publishing requires review execution")
        if self.environment == "production" and self.review_publish_enabled:
            raise ValueError("Phase 4 publishing is forbidden in production")
        marker_sources = sum(
            value is not None for value in (self.publish_marker_key, self.publish_marker_key_file)
        )
        if self.review_publish_enabled and marker_sources != 1:
            raise ValueError("publishing requires exactly one marker-key source")
        if (
            self.publish_marker_key_file is not None
            and not self.publish_marker_key_file.is_absolute()
        ):
            raise ValueError("marker-key file must be absolute")
        if self.admission_token_ceiling < 1:
            raise ValueError("review token safety margin leaves no admission budget")
        if self.review_summary_confidence > self.review_inline_confidence:
            raise ValueError("summary confidence cannot exceed inline confidence")
        return self
