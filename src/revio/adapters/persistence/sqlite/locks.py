"""Linux local-filesystem maintenance exclusion."""

import fcntl
import os
import stat
import time
from pathlib import Path
from types import TracebackType
from typing import TextIO

from revio.adapters.persistence.sqlite.connection import (
    path_has_symlink_component,
    require_local_filesystem,
)
from revio.errors import PersistenceUnavailableError


class MaintenanceLock:
    """Hold an OS-owned shared runtime or exclusive maintenance lock."""

    def __init__(self, path: Path, *, exclusive: bool, timeout_seconds: float = 2.0) -> None:
        self._path = path
        self._exclusive = exclusive
        self._timeout = timeout_seconds
        self._handle: TextIO | None = None

    def acquire(self) -> "MaintenanceLock":
        parent = self._path.parent
        if path_has_symlink_component(self._path):
            raise PersistenceUnavailableError("database lock policy is not satisfied") from None
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
        require_local_filesystem(parent)
        parent_stat = parent.stat(follow_symlinks=False)
        if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise PersistenceUnavailableError("database lock policy is not satisfied") from None
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(self._path, flags, 0o600)
            file_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) & 0o077
            ):
                raise OSError("unsafe lock file")
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            descriptor = None
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            raise PersistenceUnavailableError("database lock is unavailable") from None
        operation = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise PersistenceUnavailableError(
                        "database maintenance lock is unavailable"
                    ) from None
                time.sleep(0.05)

    def release(self) -> None:
        handle = self._handle
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._handle = None

    def __enter__(self) -> "MaintenanceLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class WorkerInstanceLock(MaintenanceLock):
    """Exclusive process-lifetime lock allowing exactly one worker."""

    def __init__(self, path: Path, *, timeout_seconds: float = 2.0) -> None:
        super().__init__(path, exclusive=True, timeout_seconds=timeout_seconds)
