"""SQLite readiness, integrity, and bounded operator status."""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy, is_busy
from revio.adapters.persistence.sqlite.schema import SCHEMA_REVISION
from revio.adapters.persistence.sqlite.values import datetime_value
from revio.errors import PersistenceUnavailableError


class SQLiteReadinessRepository:
    def __init__(self, connections: SQLiteConnectionPolicy) -> None:
        self._connections = connections
        self._last_ready: bool | None = None

    async def check_capabilities(self) -> bool:
        return await self._check(include_integrity=False)

    async def check_ready(self) -> bool:
        ready = await self._check(include_integrity=True)
        if ready != self._last_ready:
            self._connections.metric("readiness_change", outcome="ready" if ready else "not_ready")
            self._last_ready = ready
        return ready

    async def _check(self, *, include_integrity: bool) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            revisions = {
                cast(str, row["version_num"])
                for row in await connection.execute_fetchall(
                    "SELECT version_num FROM alembic_version"
                )
            }
            if revisions != {SCHEMA_REVISION}:
                return False
            if include_integrity:
                integrity_queries = (
                    "SELECT 1 FROM webhook_deliveries d "
                    "JOIN webhook_delivery_tombstones t "
                    "ON d.provider_id=t.provider_id "
                    "AND d.delivery_identity=t.delivery_identity LIMIT 1",
                    "SELECT 1 FROM webhook_deliveries d WHERE d.disposition='accepted' "
                    "AND d.normalized_event_json IS NOT NULL "
                    "AND json_valid(d.normalized_event_json) AND ("
                    "(json_extract(d.normalized_event_json,'$.event_type')='change_request' "
                    "AND d.linked_job_id IS NULL) OR "
                    "(json_extract(d.normalized_event_json,'$.event_type')='installation' "
                    "AND d.linked_job_id IS NOT NULL)) LIMIT 1",
                    "SELECT 1 FROM webhook_deliveries d "
                    "WHERE d.normalized_event_json IS NOT NULL AND ("
                    "NOT json_valid(d.normalized_event_json) OR "
                    "json_extract(d.normalized_event_json,'$.provider_id.value') "
                    "IS NOT d.provider_id OR "
                    "json_extract(d.normalized_event_json,'$.delivery_identity') "
                    "IS NOT d.delivery_identity OR "
                    "json_extract(d.normalized_event_json,'$.semantic_identity') "
                    "IS NOT d.semantic_identity) LIMIT 1",
                    "SELECT 1 FROM webhook_deliveries d "
                    "LEFT JOIN queue_jobs q ON q.id=d.linked_job_id "
                    "WHERE d.linked_job_id IS NOT NULL AND ("
                    "d.disposition<>'accepted' OR d.event_schema_version IS NOT 1 "
                    "OR d.normalized_event_json IS NULL "
                    "OR NOT json_valid(d.normalized_event_json) "
                    "OR d.semantic_identity IS NULL OR q.id IS NULL "
                    "OR q.job_type<>'change_request_validation' "
                    "OR q.provider_id<>d.provider_id "
                    "OR q.semantic_identity<>d.semantic_identity "
                    "OR q.event_schema_version IS NOT d.event_schema_version "
                    "OR q.event_json IS NOT d.normalized_event_json "
                    "OR NOT json_valid(q.event_json)) LIMIT 1",
                    "SELECT 1 FROM installation_states s "
                    "LEFT JOIN webhook_deliveries d ON d.id=s.source_delivery_id "
                    "WHERE d.id IS NULL OR d.provider_id<>s.provider_id "
                    "OR d.event_name<>'installation' OR d.disposition<>'accepted' "
                    "OR d.event_schema_version IS NOT 1 OR d.normalized_event_json IS NULL "
                    "OR NOT json_valid(d.normalized_event_json) "
                    "OR json_extract(d.normalized_event_json,'$.event_type')<>'installation' "
                    "OR json_extract(d.normalized_event_json,'$.delivery_identity')"
                    "<>d.delivery_identity "
                    "OR json_extract(d.normalized_event_json,'$.semantic_identity')"
                    "<>d.semantic_identity "
                    "OR json_extract(d.normalized_event_json,"
                    "'$.installation.external_id')<>s.installation_id "
                    "OR s.state<>CASE json_extract(d.normalized_event_json,'$.trigger') "
                    "WHEN 'created' THEN 'active' WHEN 'unsuspend' THEN 'active' "
                    "WHEN 'suspend' THEN 'suspended' WHEN 'deleted' THEN 'deleted' "
                    "ELSE NULL END LIMIT 1",
                )
                for query in integrity_queries:
                    if list(await connection.execute_fetchall(query)):
                        return False
            cursor = await connection.execute(
                "UPDATE queue_jobs SET updated_at=updated_at WHERE 0 RETURNING id"
            )
            return await cursor.fetchone() is None

        try:
            return await self._connections.write(operation, commit=False)
        except PersistenceUnavailableError:
            return False
        except aiosqlite.OperationalError as error:
            if is_busy(error):
                return False
            return False
        except (aiosqlite.Error, OSError):
            return False

    async def status(self) -> dict[str, int | float]:
        async def read(
            connection: aiosqlite.Connection,
        ) -> tuple[list[aiosqlite.Row], int, int, str | None]:
            rows = list(
                await connection.execute_fetchall(
                    "SELECT state, COUNT(*) AS total FROM queue_jobs GROUP BY state"
                )
            )
            deliveries = next(
                iter(
                    await connection.execute_fetchall(
                        "SELECT COUNT(*) AS total FROM webhook_deliveries"
                    )
                )
            )["total"]
            tombstones = next(
                iter(
                    await connection.execute_fetchall(
                        "SELECT COUNT(*) AS total FROM webhook_delivery_tombstones"
                    )
                )
            )["total"]
            oldest = next(
                iter(
                    await connection.execute_fetchall(
                        "SELECT MIN(created_at) AS oldest FROM queue_jobs WHERE state='pending'"
                    )
                )
            )["oldest"]
            return rows, cast(int, deliveries), cast(int, tombstones), oldest

        rows, deliveries, tombstones, oldest = await self._connections.read(read)
        result: dict[str, int | float] = {
            cast(str, row["state"]): cast(int, row["total"]) for row in rows
        }
        result["deliveries"] = deliveries
        result["tombstones"] = tombstones
        result["active_jobs"] = sum(
            int(result.get(state, 0)) for state in ("pending", "running", "retry_wait")
        )
        result["terminal_jobs"] = sum(
            int(result.get(state, 0)) for state in ("completed", "dead", "cancelled", "superseded")
        )
        oldest_at = datetime_value(oldest)
        result["oldest_pending_age_seconds"] = (
            max(0.0, (datetime.now(UTC) - oldest_at).total_seconds())
            if oldest_at is not None
            else 0.0
        )
        result["main_database_bytes"] = self._size(self._connections.path)
        result["wal_bytes"] = self._size(
            self._connections.path.with_name(self._connections.path.name + "-wal")
        )
        result["shm_bytes"] = self._size(
            self._connections.path.with_name(self._connections.path.name + "-shm")
        )
        for name in (
            "active_jobs",
            "terminal_jobs",
            "oldest_pending_age_seconds",
            "main_database_bytes",
            "wal_bytes",
            "shm_bytes",
        ):
            if self._connections.metrics is not None:
                self._connections.metrics.gauge(name, result[name])
        return result

    @staticmethod
    def _size(path: Path) -> int:
        failed = False
        size = 0
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            failed = True
        if failed:
            raise PersistenceUnavailableError("database status is unavailable")
        return size
