"""Phase 3 configuration and non-configurable capability tests."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from revio.adapters.persistence.sqlite.capabilities import MINIMUM_SQLITE_VERSION
from revio.adapters.persistence.sqlite.store import SQLiteStore
from revio.config.database import DatabaseSettings
from revio.config.queue import QueueSettings
from revio.errors import PersistenceUnavailableError


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
    with pytest.raises(ValidationError):
        DatabaseSettings.model_validate(
            {
                "database_path": tmp_path / "revio.db",
                "database_require_current_migration": False,
            }
        )


def test_lease_heartbeat_and_single_worker_are_validated() -> None:
    with pytest.raises(ValidationError):
        QueueSettings(queue_lease_seconds=60, queue_heartbeat_seconds=30)
    with pytest.raises(ValidationError):
        QueueSettings(queue_worker_concurrency=2)
    with pytest.raises(ValidationError):
        QueueSettings(queue_shutdown_timeout_seconds=36)


def test_compose_grace_exceeds_supported_worker_timeout() -> None:
    compose = Path("docker-compose.yml").read_text()
    assert "stop_signal: SIGTERM" in compose
    assert "stop_grace_period: 40s" in compose
    assert QueueSettings().queue_shutdown_timeout_seconds < 40


@pytest.mark.asyncio
async def test_database_path_rejects_symlinks_and_insecure_modes(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.touch(mode=0o600)
    database_link = tmp_path / "linked.db"
    database_link.symlink_to(target)
    with pytest.raises(PersistenceUnavailableError):
        await SQLiteStore(DatabaseSettings(database_path=database_link)).initialize()

    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(PersistenceUnavailableError):
        await SQLiteStore(DatabaseSettings(database_path=linked_parent / "revio.db")).initialize()

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    with pytest.raises(PersistenceUnavailableError):
        await SQLiteStore(DatabaseSettings(database_path=unsafe_parent / "revio.db")).initialize()

    unsafe_file = tmp_path / "unsafe.db"
    unsafe_file.touch(mode=0o644)
    with pytest.raises(PersistenceUnavailableError):
        await SQLiteStore(DatabaseSettings(database_path=unsafe_file)).initialize()


@pytest.mark.asyncio
async def test_safe_preexisting_database_path_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "safe.db"
    path.touch(mode=0o600)
    store = SQLiteStore(DatabaseSettings(database_path=path))
    await store.initialize()
    assert await store.check_ready()
