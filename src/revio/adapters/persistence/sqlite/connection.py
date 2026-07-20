"""Verified SQLite connection policy and deterministic write transactions."""

import asyncio
import os
import stat
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

from revio.adapters.persistence.sqlite.capabilities import require_supported_sqlite
from revio.config.database import DatabaseSettings
from revio.errors import PersistenceUnavailableError
from revio.ports.observability import QueueMetricsPort

T = TypeVar("T")
FailureHook = Callable[[str], None]
WriteOperation = Callable[[aiosqlite.Connection], Awaitable[T]]


def is_busy(error: aiosqlite.OperationalError) -> bool:
    """Recognize only SQLite lock-pressure errors."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


def path_has_symlink_component(path: Path) -> bool:
    return any(component.is_symlink() for component in (path, *path.parents))


def require_local_filesystem(path: Path) -> None:
    """Reject known network filesystem mounts in the supported Linux deployment."""
    if sys.platform != "linux":
        return
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        raise PersistenceUnavailableError("database filesystem policy is unavailable") from None
    resolved = path.resolve()
    matches: list[tuple[int, str]] = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem_fields = after.split()
        if not separator or len(fields) < 5 or not filesystem_fields:
            continue
        mount = Path(fields[4].replace("\\040", " "))
        try:
            resolved.relative_to(mount)
        except ValueError:
            continue
        matches.append((len(mount.parts), filesystem_fields[0]))
    if not matches:
        raise PersistenceUnavailableError("database filesystem policy is unavailable") from None
    filesystem = max(matches)[1]
    network_filesystems = {
        "9p",
        "ceph",
        "cifs",
        "glusterfs",
        "nfs",
        "nfs4",
        "smb3",
        "sshfs",
    }
    if filesystem in network_filesystems or filesystem.startswith("fuse.sshfs"):
        raise PersistenceUnavailableError("database filesystem is unsupported") from None


def prepare_database_file(settings: DatabaseSettings) -> Path:
    """Create or validate a controlled database directory and file."""
    path = settings.database_path
    parent = path.parent
    if path_has_symlink_component(path):
        raise PersistenceUnavailableError("database storage policy is not satisfied") from None
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    require_local_filesystem(parent)
    parent_stat = parent.stat(follow_symlinks=False)
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise PersistenceUnavailableError("database storage policy is not satisfied") from None
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise PersistenceUnavailableError("database storage policy is not satisfied") from None
    if path.exists():
        file_stat = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            raise PersistenceUnavailableError("database storage policy is not satisfied") from None
        return path
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise PersistenceUnavailableError("database storage is unavailable") from None
    os.close(descriptor)
    return path


def apply_sync_connection_policy(connection: Any, settings: DatabaseSettings) -> None:
    """Apply the verified policy to an Alembic synchronous connection."""
    require_supported_sqlite()
    foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    foreign_keys.close()
    connection.exec_driver_sql(f"PRAGMA busy_timeout={settings.database_busy_timeout_ms}").close()
    journal = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
    connection.exec_driver_sql("PRAGMA synchronous=FULL").close()
    synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar()
    foreign_key_value = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    if str(journal).lower() != "wal" or synchronous != 2 or foreign_key_value != 1:
        raise PersistenceUnavailableError("database connection policy is not satisfied") from None


class SQLiteConnectionPolicy:
    """Own connection setup and retry only lock acquisition before a transaction."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        metrics: QueueMetricsPort | None = None,
        failure_hook: FailureHook | None = None,
    ) -> None:
        require_supported_sqlite()
        self.settings = settings
        self.path = settings.database_path
        self.metrics = metrics
        self.failure_hook = failure_hook

    def fail(self, stage: str) -> None:
        if self.failure_hook is not None:
            self.failure_hook(stage)

    def metric(self, name: str, *, outcome: str | None = None) -> None:
        if self.metrics is not None:
            self.metrics.increment(name, outcome=outcome)

    @asynccontextmanager
    async def connect(
        self, *, busy_timeout_ms: int | None = None
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        prepare_database_file(self.settings)
        try:
            connection = await aiosqlite.connect(self.path, timeout=0)
        except (aiosqlite.Error, OSError):
            raise PersistenceUnavailableError("database connection is unavailable") from None
        connection.row_factory = aiosqlite.Row
        timeout = (
            self.settings.database_busy_timeout_ms if busy_timeout_ms is None else busy_timeout_ms
        )
        try:
            try:
                await connection.execute("PRAGMA foreign_keys=ON")
                await connection.execute(f"PRAGMA busy_timeout={max(1, timeout)}")
                journal = list(await connection.execute_fetchall("PRAGMA journal_mode=WAL"))
                await connection.execute("PRAGMA synchronous=FULL")
                synchronous = list(await connection.execute_fetchall("PRAGMA synchronous"))
                foreign_keys = list(await connection.execute_fetchall("PRAGMA foreign_keys"))
            except aiosqlite.Error:
                raise PersistenceUnavailableError(
                    "database connection policy is unavailable"
                ) from None
            if (
                not journal
                or str(journal[0][0]).lower() != "wal"
                or not synchronous
                or synchronous[0][0] != 2
                or not foreign_keys
                or foreign_keys[0][0] != 1
            ):
                raise PersistenceUnavailableError(
                    "database connection policy is not satisfied"
                ) from None
            yield connection
        finally:
            try:
                await self._settle(connection.close)
            except asyncio.CancelledError:
                raise
            except aiosqlite.Error:
                raise PersistenceUnavailableError(
                    "database connection cleanup failed safely"
                ) from None

    async def _settle(self, operation: Callable[[], Coroutine[Any, Any, None]]) -> None:
        task = asyncio.create_task(operation())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _rollback_after_cancellation(self, connection: aiosqlite.Connection) -> None:
        task = asyncio.create_task(connection.rollback())
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await task

    async def write(self, operation: WriteOperation[T], *, commit: bool = True) -> T:
        """Retry bounded BEGIN IMMEDIATE acquisition; never time out an active commit."""
        started = time.monotonic()
        maximum = self.settings.database_busy_max_elapsed_seconds
        for attempt in range(1, self.settings.database_busy_max_attempts + 1):
            remaining = maximum - (time.monotonic() - started)
            if remaining <= 0:
                break
            effective_ms = max(
                1,
                min(
                    self.settings.database_busy_timeout_ms,
                    int(remaining * 1_000),
                ),
            )
            async with self.connect(busy_timeout_ms=effective_ms) as connection:
                try:
                    self.fail("before_begin")
                    await connection.execute("BEGIN IMMEDIATE")
                except asyncio.CancelledError:
                    await self._rollback_after_cancellation(connection)
                    raise
                except aiosqlite.OperationalError as error:
                    if not is_busy(error):
                        raise PersistenceUnavailableError(
                            "database operation failed safely"
                        ) from None
                    if attempt >= self.settings.database_busy_max_attempts:
                        break
                    remaining = maximum - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    delay = min(0.05 * attempt, remaining)
                    if delay >= remaining:
                        break
                    await asyncio.sleep(delay)
                    continue

                try:
                    self.fail("after_begin")
                    result = await operation(connection)
                    self.fail("before_commit")
                except asyncio.CancelledError:
                    await self._rollback_after_cancellation(connection)
                    raise
                except BaseException as error:
                    await self._settle(connection.rollback)
                    if isinstance(error, aiosqlite.Error):
                        raise PersistenceUnavailableError(
                            "database operation failed safely"
                        ) from None
                    raise

                try:
                    await self._settle(connection.commit if commit else connection.rollback)
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    await self._settle(connection.rollback)
                    if isinstance(error, aiosqlite.Error):
                        raise PersistenceUnavailableError(
                            "database operation failed safely"
                        ) from None
                    raise
                return result

        self.metric("database_busy_exhausted")
        raise PersistenceUnavailableError("database is temporarily unavailable") from None
