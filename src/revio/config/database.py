"""Durable SQLite configuration."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIO_", env_ignore_empty=True, extra="ignore")

    environment: Literal["local", "sandbox", "production"] = "local"
    database_path: Path = Path("/var/lib/revio/revio.db")
    database_wal_enabled: bool = True
    database_synchronous: Literal["FULL", "NORMAL"] = "FULL"
    database_busy_timeout_ms: int = Field(default=500, ge=1, le=5_000)
    database_busy_max_attempts: int = Field(default=3, ge=1, le=10)
    database_busy_max_elapsed_seconds: float = Field(default=2.0, gt=0, le=30)
    database_require_current_migration: bool = True
    retention_terminal_age_days: int = Field(default=30, ge=1)
    retention_batch_size: int = Field(default=500, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_database(self) -> "DatabaseSettings":
        if not self.database_path.is_absolute():
            raise ValueError("database path must be absolute")
        if self.environment == "production" and not self.database_wal_enabled:
            raise ValueError("WAL is required in production")
        if self.environment == "production" and self.database_synchronous != "FULL":
            raise ValueError("synchronous FULL is required in production")
        return self
