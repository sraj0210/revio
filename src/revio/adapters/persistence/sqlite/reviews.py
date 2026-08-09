"""Durable Phase 4 review, provider-call, artifact, and write-operation state."""

import json
from datetime import datetime
from typing import Literal

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import datetime_value, timestamp
from revio.domain.models import Finding
from revio.domain.reviews import (
    NormalizedReviewArtifact,
    PartialReason,
    ProviderCallIdentity,
    ProviderCallRecord,
    ProviderCallState,
    ReviewRunRecord,
    ReviewRunState,
    UsageDisposition,
    WriteOperationRecord,
    WriteOperationState,
)
from revio.errors import PersistenceIntegrityError


def _run(row: aiosqlite.Row) -> ReviewRunRecord:
    return ReviewRunRecord(
        id=row["id"],
        job_id=row["job_id"],
        state=row["state"],
        validated_head_sha=row["validated_head_sha"],
        validated_base_sha=row["validated_base_sha"],
        check_run_external_id=row["check_run_external_id"],
        provider_check_run_id=row["provider_check_run_id"],
        terminal_reason=row["terminal_reason"],
    )


class SQLiteReviewRepository:
    def __init__(self, connections: SQLiteConnectionPolicy) -> None:
        self._connections = connections

    async def create_run(
        self,
        *,
        run_id: str,
        job_id: str,
        head_sha: str,
        base_sha: str,
        external_id: str,
        now: datetime,
    ) -> ReviewRunRecord:
        stamp = timestamp(now)

        async def operation(connection: aiosqlite.Connection) -> ReviewRunRecord:
            await connection.execute(
                "INSERT INTO review_runs (id, job_id, state, validated_head_sha, "
                "validated_base_sha, check_run_external_id, created_at, updated_at) "
                "VALUES (?, ?, 'generation_pending', ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO NOTHING",
                (run_id, job_id, head_sha, base_sha, external_id, stamp, stamp),
            )
            row = await (
                await connection.execute("SELECT * FROM review_runs WHERE job_id=?", (job_id,))
            ).fetchone()
            if row is None or row["validated_head_sha"] != head_sha:
                raise PersistenceIntegrityError("review run identity conflicts with current head")
            return _run(row)

        return await self._connections.write(operation)

    async def get_run(self, run_id: str) -> ReviewRunRecord | None:
        async def operation(connection: aiosqlite.Connection) -> ReviewRunRecord | None:
            row = await (
                await connection.execute("SELECT * FROM review_runs WHERE id=?", (run_id,))
            ).fetchone()
            return _run(row) if row is not None else None

        return await self._connections.read(operation)

    async def transition_run(
        self,
        run_id: str,
        expected: ReviewRunState,
        target: ReviewRunState,
        now: datetime,
        *,
        reason: str | None = None,
    ) -> bool:
        allowed = {
            ReviewRunState.GENERATION_PENDING: {
                ReviewRunState.GENERATION_ATTEMPTED,
                ReviewRunState.ARTIFACT_DURABLE,
                ReviewRunState.CHECK_RUN_INDETERMINATE,
            },
            ReviewRunState.GENERATION_ATTEMPTED: {
                ReviewRunState.ARTIFACT_DURABLE,
                ReviewRunState.CHECK_RUN_INDETERMINATE,
            },
            ReviewRunState.ARTIFACT_DURABLE: {
                ReviewRunState.PUBLISHING,
                ReviewRunState.COMPLETED,
                ReviewRunState.PARTIAL,
            },
            ReviewRunState.PUBLISHING: {
                ReviewRunState.COMPLETED,
                ReviewRunState.PARTIAL,
                ReviewRunState.SUPERSEDED,
                ReviewRunState.PUBLICATION_INDETERMINATE,
                ReviewRunState.CHECK_RUN_INDETERMINATE,
            },
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal review-run state transition")

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE review_runs SET state=?, terminal_reason=?, updated_at=? "
                "WHERE id=? AND state=?",
                (target, reason, timestamp(now), run_id, expected),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def reserve_call(
        self, identity: ProviderCallIdentity, now: datetime
    ) -> ProviderCallRecord:
        stamp = timestamp(now)

        async def operation(connection: aiosqlite.Connection) -> ProviderCallRecord:
            await connection.execute(
                "INSERT INTO provider_calls "
                "(id, review_run_id, call_kind, call_ordinal, provider_id, "
                "model_profile_id, model_profile_version, prompt_version, schema_version, "
                "estimated_input_tokens, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?) "
                "ON CONFLICT(review_run_id, call_kind, call_ordinal) DO NOTHING",
                (
                    identity.id,
                    identity.review_run_id,
                    identity.call_kind,
                    identity.call_ordinal,
                    identity.provider_id,
                    identity.model_profile_id,
                    identity.model_profile_version,
                    identity.prompt_version,
                    identity.schema_version,
                    identity.estimated_input_tokens,
                    stamp,
                    stamp,
                ),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM provider_calls WHERE review_run_id=? "
                    "AND call_kind=? AND call_ordinal=?",
                    (identity.review_run_id, identity.call_kind, identity.call_ordinal),
                )
            ).fetchone()
            if row is None or self._call(row).identity != identity:
                raise PersistenceIntegrityError("provider call identity conflict")
            return self._call(row)

        return await self._connections.write(operation)

    @staticmethod
    def _call(row: aiosqlite.Row) -> ProviderCallRecord:
        identity = ProviderCallIdentity(
            id=row["id"],
            review_run_id=row["review_run_id"],
            call_kind=row["call_kind"],
            call_ordinal=row["call_ordinal"],
            provider_id=row["provider_id"],
            model_profile_id=row["model_profile_id"],
            model_profile_version=row["model_profile_version"],
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            estimated_input_tokens=row["estimated_input_tokens"],
        )
        created = datetime_value(row["created_at"])
        updated = datetime_value(row["updated_at"])
        assert created is not None and updated is not None
        return ProviderCallRecord(
            identity=identity, state=row["state"], created_at=created, updated_at=updated
        )

    async def get_call(self, call_id: str) -> ProviderCallRecord | None:
        async def operation(connection: aiosqlite.Connection) -> ProviderCallRecord | None:
            row = await (
                await connection.execute("SELECT * FROM provider_calls WHERE id=?", (call_id,))
            ).fetchone()
            return self._call(row) if row is not None else None

        return await self._connections.read(operation)

    async def latest_call(
        self, run_id: str, call_kind: Literal["initial", "repair"]
    ) -> ProviderCallRecord | None:
        async def operation(connection: aiosqlite.Connection) -> ProviderCallRecord | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM provider_calls WHERE review_run_id=? AND call_kind=? "
                    "ORDER BY call_ordinal DESC LIMIT 1",
                    (run_id, call_kind),
                )
            ).fetchone()
            return self._call(row) if row is not None else None

        return await self._connections.read(operation)

    async def transition_call(
        self,
        call_id: str,
        expected: ProviderCallState,
        target: ProviderCallState,
        now: datetime,
        *,
        usage: UsageDisposition | None = None,
    ) -> bool:
        allowed = {
            ProviderCallState.RESERVED: {ProviderCallState.ATTEMPT_STARTED},
            ProviderCallState.ATTEMPT_STARTED: {
                ProviderCallState.RESPONSE_OBSERVED,
                ProviderCallState.AMBIGUOUS,
                ProviderCallState.KNOWN_REJECTED,
            },
            ProviderCallState.RESPONSE_OBSERVED: {ProviderCallState.COMPLETED},
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal provider-call state transition")

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE provider_calls SET state=?, updated_at=? WHERE id=? AND state=?",
                (target, timestamp(now), call_id, expected),
            )
            if cursor.rowcount != 1:
                return False
            if usage is not None:
                values = usage.usage
                await connection.execute(
                    "INSERT INTO provider_usage (provider_call_id, usage_status, "
                    "uncached_input_tokens, cached_input_tokens, cache_creation_tokens, "
                    "output_tokens, "
                    "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_id,
                        usage.status,
                        values.uncached_input_tokens if values else None,
                        values.cached_input_tokens if values else None,
                        values.cache_creation_tokens if values else None,
                        values.output_tokens if values else None,
                        timestamp(now),
                    ),
                )
            return True

        return await self._connections.write(operation)

    async def persist_artifact(
        self,
        artifact: NormalizedReviewArtifact,
        expected_run_state: ReviewRunState,
    ) -> bool:
        findings_json = json.dumps(
            [finding.model_dump(mode="json") for finding in artifact.findings],
            sort_keys=True,
            separators=(",", ":"),
        )
        reasons_json = json.dumps([str(reason) for reason in artifact.reason_codes])

        async def operation(connection: aiosqlite.Connection) -> bool:
            await connection.execute(
                "INSERT INTO review_artifacts (review_run_id, artifact_version, schema_version, "
                "prompt_version, model_profile_id, model_profile_version, summary, findings_json, "
                "partial, reason_codes_json, artifact_digest, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(review_run_id) DO NOTHING",
                (
                    artifact.review_run_id,
                    artifact.artifact_version,
                    artifact.schema_version,
                    artifact.prompt_version,
                    artifact.model_profile_id,
                    artifact.model_profile_version,
                    artifact.summary,
                    findings_json,
                    int(artifact.partial),
                    reasons_json,
                    artifact.digest,
                    timestamp(artifact.created_at),
                    timestamp(artifact.updated_at),
                ),
            )
            row = await (
                await connection.execute(
                    "SELECT artifact_digest FROM review_artifacts WHERE review_run_id=?",
                    (artifact.review_run_id,),
                )
            ).fetchone()
            if row is None or row["artifact_digest"] != artifact.digest:
                raise PersistenceIntegrityError("review artifact identity conflict")
            cursor = await connection.execute(
                "UPDATE review_runs SET state='artifact_durable', updated_at=? "
                "WHERE id=? AND state=?",
                (timestamp(artifact.updated_at), artifact.review_run_id, expected_run_state),
            )
            if cursor.rowcount == 1:
                return True
            existing = await (
                await connection.execute(
                    "SELECT state FROM review_runs WHERE id=?", (artifact.review_run_id,)
                )
            ).fetchone()
            return existing is not None and existing["state"] in {
                "artifact_durable",
                "publishing",
                "completed",
                "partial",
                "publication_indeterminate",
                "superseded",
            }

        return await self._connections.write(operation)

    async def get_artifact(self, run_id: str) -> NormalizedReviewArtifact | None:
        async def operation(connection: aiosqlite.Connection) -> NormalizedReviewArtifact | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM review_artifacts WHERE review_run_id=?", (run_id,)
                )
            ).fetchone()
            if row is None:
                return None
            created = datetime_value(row["created_at"])
            updated = datetime_value(row["updated_at"])
            assert created is not None and updated is not None
            return NormalizedReviewArtifact(
                review_run_id=row["review_run_id"],
                artifact_version=row["artifact_version"],
                schema_version=row["schema_version"],
                prompt_version=row["prompt_version"],
                model_profile_id=row["model_profile_id"],
                model_profile_version=row["model_profile_version"],
                summary=row["summary"],
                findings=tuple(
                    Finding.model_validate(item) for item in json.loads(row["findings_json"])
                ),
                partial=bool(row["partial"]),
                reason_codes=tuple(
                    PartialReason(item) for item in json.loads(row["reason_codes_json"])
                ),
                digest=row["artifact_digest"],
                created_at=created,
                updated_at=updated,
            )

        return await self._connections.read(operation)

    async def replace_artifact(
        self, artifact: NormalizedReviewArtifact, *, expected_digest: str
    ) -> bool:
        findings_json = json.dumps(
            [finding.model_dump(mode="json") for finding in artifact.findings],
            sort_keys=True,
            separators=(",", ":"),
        )
        reasons_json = json.dumps([str(reason) for reason in artifact.reason_codes])

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE review_artifacts SET summary=?, findings_json=?, partial=?, "
                "reason_codes_json=?, artifact_digest=?, updated_at=? "
                "WHERE review_run_id=? AND artifact_digest=?",
                (
                    artifact.summary,
                    findings_json,
                    int(artifact.partial),
                    reasons_json,
                    artifact.digest,
                    timestamp(artifact.updated_at),
                    artifact.review_run_id,
                    expected_digest,
                ),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def reserve_write_operation(
        self,
        kind: Literal["check_run", "publish"],
        *,
        operation_id: str,
        run_id: str,
        head_sha: str,
        now: datetime,
        external_id: str | None = None,
        operation_key: str | None = None,
        marker: str | None = None,
        marker_key_id: str | None = None,
    ) -> WriteOperationRecord:
        table = "check_run_operations" if kind == "check_run" else "publish_operations"
        stamp = timestamp(now)

        async def operation(connection: aiosqlite.Connection) -> WriteOperationRecord:
            if kind == "check_run":
                await connection.execute(
                    "INSERT INTO check_run_operations "
                    "(id, review_run_id, external_id, head_sha, state, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 'reserved_unattempted', ?, ?) "
                    "ON CONFLICT(review_run_id) DO NOTHING",
                    (operation_id, run_id, external_id, head_sha, stamp, stamp),
                )
            else:
                await connection.execute(
                    "INSERT INTO publish_operations (id, review_run_id, operation_key, marker, "
                    "marker_key_id, head_sha, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'reserved_unattempted', ?, ?) "
                    "ON CONFLICT(review_run_id) DO NOTHING",
                    (
                        operation_id,
                        run_id,
                        operation_key,
                        marker,
                        marker_key_id,
                        head_sha,
                        stamp,
                        stamp,
                    ),
                )
            row = await (
                await connection.execute(f"SELECT * FROM {table} WHERE review_run_id=?", (run_id,))
            ).fetchone()
            if row is None or row["id"] != operation_id:
                raise PersistenceIntegrityError("write operation identity conflict")
            if row["head_sha"] != head_sha:
                raise PersistenceIntegrityError("write operation head identity conflict")
            if kind == "check_run" and row["external_id"] != external_id:
                raise PersistenceIntegrityError("Check Run operation identity conflict")
            if kind == "publish" and (
                row["operation_key"] != operation_key
                or row["marker"] != marker
                or row["marker_key_id"] != marker_key_id
            ):
                raise PersistenceIntegrityError("publish operation identity conflict")
            return self._write_record(kind, row)

        return await self._connections.write(operation)

    @staticmethod
    def _write_record(
        kind: Literal["check_run", "publish"], row: aiosqlite.Row
    ) -> WriteOperationRecord:
        provider_column = "provider_check_run_id" if kind == "check_run" else "provider_review_id"
        return WriteOperationRecord(
            id=row["id"],
            review_run_id=row["review_run_id"],
            state=row["state"],
            head_sha=row["head_sha"],
            attempt_count=row["attempt_count"],
            provider_id=row[provider_column],
            operation_key=row["operation_key"] if kind == "publish" else None,
            marker=row["marker"] if kind == "publish" else None,
            marker_key_id=row["marker_key_id"] if kind == "publish" else None,
            external_id=row["external_id"] if kind == "check_run" else None,
            terminal_reason=row["terminal_reason"],
        )

    async def transition_write_operation(
        self,
        kind: Literal["check_run", "publish"],
        operation_id: str,
        expected: WriteOperationState,
        target: WriteOperationState,
        now: datetime,
        *,
        provider_id: str | None = None,
        terminal_reason: str | None = None,
        increment_attempt: bool = False,
    ) -> bool:
        allowed = {
            WriteOperationState.RESERVED_UNATTEMPTED: {
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.RECONCILED,
                WriteOperationState.INTEGRITY_FAILED,
            },
            WriteOperationState.ATTEMPT_STARTED: {
                WriteOperationState.KNOWN_REJECTED,
                WriteOperationState.AMBIGUOUS,
                WriteOperationState.COMPLETED,
            },
            WriteOperationState.AMBIGUOUS: {
                WriteOperationState.RECONCILED,
                WriteOperationState.INTEGRITY_FAILED,
            },
            WriteOperationState.KNOWN_REJECTED: {
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.RECONCILED,
                WriteOperationState.INTEGRITY_FAILED,
            },
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal write-operation state transition")
        if (
            target in {WriteOperationState.COMPLETED, WriteOperationState.RECONCILED}
            and not provider_id
        ):
            raise PersistenceIntegrityError("terminal provider write requires provider identity")
        table = "check_run_operations" if kind == "check_run" else "publish_operations"
        provider_column = "provider_check_run_id" if kind == "check_run" else "provider_review_id"

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                f"UPDATE {table} SET state=?, {provider_column}=COALESCE(?, {provider_column}), "
                "terminal_reason=?, attempt_count=attempt_count+?, updated_at=? "
                "WHERE id=? AND state=?",
                (
                    target,
                    provider_id,
                    terminal_reason,
                    int(increment_attempt),
                    timestamp(now),
                    operation_id,
                    expected,
                ),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def unresolved_marker_key_ids(self) -> set[str]:
        async def operation(connection: aiosqlite.Connection) -> set[str]:
            rows = await (
                await connection.execute(
                    "SELECT DISTINCT marker_key_id FROM publish_operations "
                    "WHERE state NOT IN ('completed', 'reconciled', 'integrity_failed')"
                )
            ).fetchall()
            return {row["marker_key_id"] for row in rows}

        return await self._connections.read(operation)
