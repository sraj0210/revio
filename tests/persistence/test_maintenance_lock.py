"""Process-owned shared/exclusive maintenance exclusion."""

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from revio.adapters.persistence.sqlite.locks import MaintenanceLock, WorkerInstanceLock
from revio.errors import PersistenceUnavailableError


def _hold_shared(path: str, connection: Connection) -> None:
    with MaintenanceLock(Path(path), exclusive=False):
        connection.send("ready")
        connection.recv()


def _hold_worker(directory: str, connection: Connection) -> None:
    root = Path(directory)
    with MaintenanceLock(root / "revio.maintenance.lock", exclusive=False):
        with WorkerInstanceLock(root / "revio.worker.lock"):
            connection.send("ready")
            connection.recv()


def test_exclusive_maintenance_refuses_while_runtime_process_is_active(
    tmp_path: Path,
) -> None:
    parent, child = multiprocessing.Pipe()
    lock_path = tmp_path / "revio.maintenance.lock"
    process = multiprocessing.Process(target=_hold_shared, args=(str(lock_path), child))
    process.start()
    assert parent.recv() == "ready"
    with pytest.raises(PersistenceUnavailableError):
        MaintenanceLock(lock_path, exclusive=True, timeout_seconds=0.05).acquire()
    parent.send("release")
    process.join(timeout=5)
    assert process.exitcode == 0
    with MaintenanceLock(lock_path, exclusive=True, timeout_seconds=0.05):
        pass


def test_only_one_worker_process_and_process_death_releases_lock(tmp_path: Path) -> None:
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=_hold_worker, args=(str(tmp_path), child))
    process.start()
    assert parent.recv() == "ready"
    with pytest.raises(PersistenceUnavailableError):
        WorkerInstanceLock(tmp_path / "revio.worker.lock", timeout_seconds=0.05).acquire()
    with pytest.raises(PersistenceUnavailableError):
        MaintenanceLock(
            tmp_path / "revio.maintenance.lock", exclusive=True, timeout_seconds=0.05
        ).acquire()
    process.terminate()
    process.join(timeout=5)
    assert process.exitcode is not None
    with WorkerInstanceLock(tmp_path / "revio.worker.lock", timeout_seconds=0.05):
        pass
    with MaintenanceLock(tmp_path / "revio.maintenance.lock", exclusive=True, timeout_seconds=0.05):
        pass


def test_lock_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("safe")
    link = tmp_path / "revio.worker.lock"
    link.symlink_to(target)
    with pytest.raises(PersistenceUnavailableError):
        WorkerInstanceLock(link, timeout_seconds=0.05).acquire()


def test_lock_file_requires_safe_existing_regular_file(tmp_path: Path) -> None:
    safe = tmp_path / "safe.lock"
    safe.touch(mode=0o600)
    with WorkerInstanceLock(safe, timeout_seconds=0.05):
        pass

    unsafe = tmp_path / "unsafe.lock"
    unsafe.touch(mode=0o644)
    with pytest.raises(PersistenceUnavailableError):
        WorkerInstanceLock(unsafe, timeout_seconds=0.05).acquire()

    directory = tmp_path / "directory.lock"
    directory.mkdir()
    with pytest.raises(PersistenceUnavailableError):
        WorkerInstanceLock(directory, timeout_seconds=0.05).acquire()
