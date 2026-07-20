"""Process-owned shared/exclusive maintenance exclusion."""

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from revio.adapters.persistence.sqlite.locks import MaintenanceLock
from revio.errors import PersistenceUnavailableError


def _hold_shared(path: str, connection: Connection) -> None:
    with MaintenanceLock(Path(path), exclusive=False):
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
