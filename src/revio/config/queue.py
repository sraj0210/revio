"""Durable queue and worker configuration."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class QueueSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    queue_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    queue_lease_seconds: int = Field(default=60, ge=10, le=3_600)
    queue_heartbeat_seconds: int = Field(default=20, ge=1, le=1_800)
    queue_max_attempts: int = Field(default=5, ge=1, le=100)
    queue_retry_base_seconds: float = Field(default=5, gt=0, le=3_600)
    queue_retry_max_seconds: float = Field(default=300, gt=0, le=86_400)
    queue_retry_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    queue_worker_concurrency: int = Field(default=1, ge=1, le=1)
    queue_shutdown_timeout_seconds: float = Field(default=30, gt=0, le=600)
    queue_max_active_jobs: int = Field(default=10_000, ge=1, le=1_000_000)
    queue_max_event_json_bytes: int = Field(default=16_384, ge=1_024, le=1_048_576)

    @model_validator(mode="after")
    def validate_queue(self) -> "QueueSettings":
        if self.queue_heartbeat_seconds * 2 >= self.queue_lease_seconds:
            raise ValueError("queue heartbeat must be less than half the lease")
        if self.queue_retry_base_seconds > self.queue_retry_max_seconds:
            raise ValueError("queue retry base exceeds maximum")
        return self
