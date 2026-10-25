"""Administrator-controlled observability and operational-limit configuration."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VENAROQ_", env_ignore_empty=True, extra="ignore")

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "plain"] = "json"
    # Metrics are served only on a separate internal bind, never on the public webhook listener.
    # Empty disables the metrics server entirely.
    metrics_bind: str = ""
    metrics_port: int = Field(default=9090, ge=1, le=65535)
    # Per-source webhook ingress rate limit (fixed window). Bounded, fail-closed.
    ingress_rate_limit: int = Field(default=120, ge=1, le=100_000)
    ingress_rate_window_seconds: float = Field(default=60.0, gt=0, le=3600)

    @property
    def metrics_enabled(self) -> bool:
        return bool(self.metrics_bind.strip())
