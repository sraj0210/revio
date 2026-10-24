"""Durable normalized Phase 5 configuration resolution."""

import json
from datetime import datetime

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import datetime_value, timestamp
from revio.domain.configuration import ConfigurationResolution, EffectiveReviewPolicy
from revio.errors import PersistenceIntegrityError


class SQLiteConfigurationRepository:
    def __init__(self, connections: SQLiteConnectionPolicy) -> None:
        self._connections = connections

    async def get(self, job_id: str) -> ConfigurationResolution | None:
        async def operation(connection: aiosqlite.Connection) -> ConfigurationResolution | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM review_configurations WHERE job_id=?", (job_id,)
                )
            ).fetchone()
            return self._resolution(row) if row is not None else None

        return await self._connections.read(operation)

    @staticmethod
    def _resolution(row: aiosqlite.Row) -> ConfigurationResolution:
        created_at = datetime_value(row["created_at"])
        updated_at = datetime_value(row["updated_at"])
        if created_at is None or updated_at is None:
            raise PersistenceIntegrityError("configuration timestamps are missing")
        return ConfigurationResolution(
            job_id=row["job_id"],
            review_run_id=row["review_run_id"],
            provider_id=row["provider_id"],
            installation_id=row["installation_id"],
            repository_id=row["repository_id"],
            base_sha=row["base_sha"],
            status=row["config_status"],
            schema_version=row["schema_version"],
            source_digest=row["source_digest"],
            policy=EffectiveReviewPolicy(
                enabled=bool(row["normalized_enabled"]),
                focus_areas=tuple(json.loads(row["focus_areas_json"])),
                exclusions=tuple(json.loads(row["repository_exclusions_json"])),
                summary_confidence=row["effective_summary_confidence"],
                inline_confidence=row["effective_inline_confidence"],
                warning_codes=tuple(json.loads(row["warning_codes_json"])),
            ),
            created_at=created_at,
            updated_at=updated_at,
        )

    async def persist(self, resolution: ConfigurationResolution) -> ConfigurationResolution:
        policy = resolution.policy
        values = (
            resolution.job_id,
            resolution.review_run_id,
            resolution.provider_id,
            resolution.installation_id,
            resolution.repository_id,
            resolution.base_sha,
            resolution.status,
            resolution.schema_version,
            int(policy.enabled),
            json.dumps(policy.focus_areas, separators=(",", ":")),
            json.dumps(policy.exclusions, separators=(",", ":")),
            policy.summary_confidence,
            policy.inline_confidence,
            json.dumps(policy.warning_codes, separators=(",", ":")),
            resolution.source_digest,
            policy.digest,
            timestamp(resolution.created_at),
            timestamp(resolution.updated_at),
        )

        async def operation(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT INTO review_configurations (job_id,review_run_id,provider_id,"
                "installation_id,repository_id,base_sha,config_status,schema_version,"
                "normalized_enabled,focus_areas_json,repository_exclusions_json,"
                "effective_summary_confidence,effective_inline_confidence,warning_codes_json,"
                "source_digest,policy_digest,created_at,updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING",
                values,
            )

        await self._connections.write(operation)
        existing = await self.get(resolution.job_id)
        if existing is None or existing.model_dump() != resolution.model_dump():
            raise PersistenceIntegrityError("configuration resolution identity conflict")
        return existing

    async def link_run(self, job_id: str, run_id: str, now: datetime) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE review_configurations SET review_run_id=?,updated_at=? "
                "WHERE job_id=? AND (review_run_id IS NULL OR review_run_id=?)",
                (run_id, timestamp(now), job_id, run_id),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)
