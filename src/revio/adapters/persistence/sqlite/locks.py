"""Linux local-filesystem maintenance exclusion."""

import fcntl
import time
from pathlib import Path
from types import TracebackType
from typing import TextIO

from revio.errors import PersistenceUnavailableError


class MaintenanceLock:
    """Hold an OS-owned shared runtime or exclusive maintenance lock."""

    def __init__(self, path: Path, *, exclusive: bool, timeout_seconds: float = 2.0) -> None:
        self._path = path
        self._exclusive = exclusive
        self._timeout = timeout_seconds
        self._handle: TextIO | None = None

    def acquire(self) -> "MaintenanceLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
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
