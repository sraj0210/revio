"""Durable installation lifecycle state repository."""

from datetime import datetime
from typing import cast

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import datetime_value, timestamp
from revio.domain.events import ReviewEvent
from revio.domain.queue import InstallationState, InstallationStatus


class SQLiteInstallationRepository:
    def __init__(self, connections: SQLiteConnectionPolicy) -> None:
        self._connections = connections

    async def update(
        self,
        connection: aiosqlite.Connection,
        delivery_id: str,
        event: ReviewEvent,
        received_at: datetime,
    ) -> None:
        status = {
            "created": InstallationStatus.ACTIVE,
            "unsuspend": InstallationStatus.ACTIVE,
            "suspend": InstallationStatus.SUSPENDED,
            "deleted": InstallationStatus.DELETED,
        }[event.trigger]
        await connection.execute(
            """
            INSERT INTO installation_states (
                provider_id, installation_id, state, source_delivery_id,
                provider_updated_at, ordering_delivery_identity, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, installation_id) DO UPDATE SET
                state=excluded.state, source_delivery_id=excluded.source_delivery_id,
                provider_updated_at=excluded.provider_updated_at,
                ordering_delivery_identity=excluded.ordering_delivery_identity,
                updated_at=excluded.updated_at
            """,
            (
                str(event.provider_id),
                event.installation.external_id,
                status,
                delivery_id,
                timestamp(event.provider_updated_at)
                if event.provider_updated_at is not None
                else None,
                event.delivery_identity,
                timestamp(received_at),
            ),
        )
        self._connections.fail("after_lifecycle_upsert")

    async def get(self, provider_id: str, installation_id: str) -> InstallationState | None:
        async def read(connection: aiosqlite.Connection) -> list[aiosqlite.Row]:
            return list(
                await connection.execute_fetchall(
                    "SELECT * FROM installation_states WHERE provider_id=? AND installation_id=?",
                    (provider_id, installation_id),
                )
            )

        rows = await self._connections.read(read)
        if not rows:
            return None
        row = rows[0]
        return InstallationState(
            provider_id=row["provider_id"],
            installation_id=row["installation_id"],
            state=row["state"],
            provider_updated_at=datetime_value(row["provider_updated_at"]),
            ordering_delivery_identity=row["ordering_delivery_identity"],
            updated_at=cast(datetime, datetime_value(row["updated_at"])),
        )
