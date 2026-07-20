"""Phase 3 configuration and non-configurable capability tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from revio.adapters.persistence.sqlite.capabilities import MINIMUM_SQLITE_VERSION
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings


def test_busy_and_active_capacity_defaults_are_aligned() -> None:
    database = DatabaseSettings()
    queue = QueueSettings()
    assert database.database_busy_timeout_ms == 500
    assert database.database_busy_max_attempts == 3
    assert database.database_busy_max_elapsed_seconds == 2
    assert queue.queue_max_active_jobs == 10_000
    assert not hasattr(queue, "queue_max_pending_jobs")


def test_sqlite_minimum_is_a_code_capability_not_a_setting() -> None:
    assert MINIMUM_SQLITE_VERSION == (3, 35, 0)
    assert not hasattr(DatabaseSettings(), "database_minimum_sqlite_version")


def test_database_path_and_production_durability_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(database_path=Path("relative.db"))
    with pytest.raises(ValidationError):
        DatabaseSettings(
            environment="production",
            database_path=tmp_path / "revio.db",
            database_wal_enabled=False,
        )
    with pytest.raises(ValidationError):
        DatabaseSettings(
            environment="production",
            database_path=tmp_path / "revio.db",
            database_synchronous="NORMAL",
        )


def test_lease_heartbeat_and_single_worker_are_validated() -> None:
    with pytest.raises(ValidationError):
        QueueSettings(queue_lease_seconds=60, queue_heartbeat_seconds=30)
    with pytest.raises(ValidationError):
        QueueSettings(queue_worker_concurrency=2)
