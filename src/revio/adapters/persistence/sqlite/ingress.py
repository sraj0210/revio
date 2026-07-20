"""Atomic durable delivery ingress repository."""

import uuid
from datetime import datetime
from typing import cast

import aiosqlite
from pydantic import ValidationError

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.installations import SQLiteInstallationRepository
from revio.adapters.persistence.sqlite.schema import ACTIVE_JOB_INSERT_SQL
from revio.adapters.persistence.sqlite.values import timestamp
from revio.config.queue import QueueSettings
from revio.domain.events import ReviewEvent, WebhookNormalizationResult
from revio.domain.queue import IngressDisposition, IngressReceipt
from revio.errors import (
    InvalidJobError,
    PersistenceIntegrityError,
    PersistenceNotCommittedError,
    QueueCapacityError,
)


class SQLiteIngressRepository:
    def __init__(
        self,
        connections: SQLiteConnectionPolicy,
        installations: SQLiteInstallationRepository,
        queue: QueueSettings,
    ) -> None:
        self._connections = connections
        self._installations = installations
        self._queue = queue

    @staticmethod
    def _lifecycle_state(event: ReviewEvent) -> str | None:
        return {
            "created": "active",
            "unsuspend": "active",
            "suspend": "suspended",
            "deleted": "deleted",
        }.get(event.trigger)

    @staticmethod
    def _decode_lifecycle(value: object) -> ReviewEvent | None:
        if not isinstance(value, str):
            return None
        try:
            event = ReviewEvent.model_validate_json(value)
        except (TypeError, ValueError, ValidationError):
            return None
        return event if event.event_type == "installation" else None

    async def persist(
        self,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
    ) -> IngressReceipt:
        signals: list[tuple[str, str | None]] = []

        async def operation(connection: aiosqlite.Connection) -> IngressReceipt:
            return await self._persist_transaction(
                connection,
                provider_id=provider_id,
                delivery_identity=delivery_identity,
                event_name=event_name,
                payload_sha256=payload_sha256,
                normalization=normalization,
                received_at=received_at,
                signals=signals,
            )

        async def reconcile(expected: IngressReceipt) -> IngressReceipt:
            event = normalization.event
            expected_event_json = event.model_dump_json() if event is not None else None

            async def read(connection: aiosqlite.Connection) -> IngressReceipt:
                tombstones = list(
                    await connection.execute_fetchall(
                        "SELECT payload_sha256 FROM webhook_delivery_tombstones "
                        "WHERE provider_id=? AND delivery_identity=?",
                        (provider_id, delivery_identity),
                    )
                )
                deliveries = list(
                    await connection.execute_fetchall(
                        "SELECT id, provider_id, delivery_identity, payload_sha256, disposition, "
                        "linked_job_id, event_name, "
                        "event_schema_version, normalized_event_json, semantic_identity "
                        "FROM webhook_deliveries "
                        "WHERE provider_id=? AND delivery_identity=?",
                        (provider_id, delivery_identity),
                    )
                )
                if tombstones and deliveries:
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if tombstones:
                    disposition = (
                        IngressDisposition.IDEMPOTENT
                        if tombstones[0]["payload_sha256"] == payload_sha256
                        else IngressDisposition.CONFLICT
                    )
                    if (
                        expected.disposition
                        not in {
                            IngressDisposition.IDEMPOTENT,
                            IngressDisposition.CONFLICT,
                        }
                        or disposition != expected.disposition
                    ):
                        raise PersistenceIntegrityError(
                            "durable ingress reconciliation found inconsistent state"
                        )
                    return IngressReceipt(disposition=disposition)
                if not deliveries:
                    if event is not None:
                        partial = list(
                            await connection.execute_fetchall(
                                "SELECT 1 FROM webhook_deliveries WHERE "
                                "normalized_event_json=? OR semantic_identity=? "
                                "OR linked_job_id=? LIMIT 1",
                                (expected_event_json, event.semantic_identity, expected.job_id),
                            )
                        )
                        if partial:
                            raise PersistenceIntegrityError(
                                "durable ingress reconciliation found inconsistent state"
                            )
                    raise PersistenceNotCommittedError("durable ingress commit was not confirmed")
                delivery = deliveries[0]
                if (
                    delivery["provider_id"] != provider_id
                    or delivery["delivery_identity"] != delivery_identity
                ):
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if delivery["payload_sha256"] != payload_sha256:
                    if expected.disposition == IngressDisposition.CONFLICT:
                        return expected
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if expected.disposition == IngressDisposition.CONFLICT:
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if delivery["disposition"] != normalization.disposition:
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if (
                    delivery["event_name"] != event_name
                    or delivery["event_schema_version"] != (1 if event is not None else None)
                    or delivery["normalized_event_json"] != expected_event_json
                    or delivery["semantic_identity"]
                    != (event.semantic_identity if event is not None else None)
                    or cast(str | None, delivery["linked_job_id"]) != expected.job_id
                ):
                    raise PersistenceIntegrityError(
                        "durable ingress reconciliation found inconsistent state"
                    )
                if expected.job_id is not None:
                    jobs = list(
                        await connection.execute_fetchall(
                            "SELECT job_type, semantic_identity, event_schema_version, event_json "
                            "FROM queue_jobs WHERE id=?",
                            (expected.job_id,),
                        )
                    )
                    if (
                        not jobs
                        or event is None
                        or jobs[0]["job_type"] != "change_request_validation"
                        or jobs[0]["semantic_identity"] != event.semantic_identity
                        or jobs[0]["event_schema_version"] != 1
                        or jobs[0]["event_json"] != expected_event_json
                    ):
                        raise PersistenceIntegrityError(
                            "durable ingress reconciliation found inconsistent state"
                        )
                if event is not None and event.event_type == "installation":
                    states = list(
                        await connection.execute_fetchall(
                            "SELECT state, source_delivery_id FROM installation_states "
                            "WHERE provider_id=? AND installation_id=?",
                            (provider_id, event.installation.external_id),
                        )
                    )
                    if not states:
                        raise PersistenceIntegrityError(
                            "durable ingress reconciliation found inconsistent state"
                        )
                    state = states[0]
                    expected_state = self._lifecycle_state(event)
                    if state["source_delivery_id"] == delivery["id"]:
                        if expected_state is None or state["state"] != expected_state:
                            raise PersistenceIntegrityError(
                                "durable ingress reconciliation found inconsistent state"
                            )
                    else:
                        sources = list(
                            await connection.execute_fetchall(
                                "SELECT provider_id, delivery_identity, event_name, disposition, "
                                "event_schema_version, normalized_event_json, semantic_identity "
                                "FROM webhook_deliveries WHERE id=?",
                                (state["source_delivery_id"],),
                            )
                        )
                        source_event = (
                            self._decode_lifecycle(sources[0]["normalized_event_json"])
                            if sources
                            else None
                        )
                        if (
                            not sources
                            or source_event is None
                            or sources[0]["provider_id"] != provider_id
                            or sources[0]["delivery_identity"] != source_event.delivery_identity
                            or sources[0]["event_name"] != "installation"
                            or sources[0]["disposition"] != IngressDisposition.ACCEPTED
                            or sources[0]["event_schema_version"] != 1
                            or sources[0]["semantic_identity"] != source_event.semantic_identity
                            or source_event.installation != event.installation
                            or self._lifecycle_state(source_event) != state["state"]
                        ):
                            raise PersistenceIntegrityError(
                                "durable ingress reconciliation found inconsistent state"
                            )
                return expected

            return await self._connections.read(read)

        try:
            receipt = await self._connections.write(operation, reconcile=reconcile)
        except QueueCapacityError:
            self._connections.metric("capacity_rejection")
            raise
        for metric, outcome in signals:
            self._connections.metric(metric, outcome=outcome)
        return receipt

    async def _persist_transaction(
        self,
        connection: aiosqlite.Connection,
        *,
        provider_id: str,
        delivery_identity: str,
        event_name: str,
        payload_sha256: str,
        normalization: WebhookNormalizationResult,
        received_at: datetime,
        signals: list[tuple[str, str | None]],
    ) -> IngressReceipt:
        tombstones = list(
            await connection.execute_fetchall(
                "SELECT payload_sha256 FROM webhook_delivery_tombstones "
                "WHERE provider_id=? AND delivery_identity=?",
                (provider_id, delivery_identity),
            )
        )
        self._connections.fail("after_tombstone_lookup")
        if tombstones:
            if tombstones[0]["payload_sha256"] == payload_sha256:
                signals.append(("delivery_exact_duplicate", None))
                return IngressReceipt(disposition=IngressDisposition.IDEMPOTENT)
            signals.append(("delivery_integrity_conflict", None))
            return IngressReceipt(disposition=IngressDisposition.CONFLICT)

        delivery_id = uuid.uuid4().hex
        event = normalization.event
        event_json = event.model_dump_json() if event is not None else None
        if (
            event_json is not None
            and len(event_json.encode()) > self._queue.queue_max_event_json_bytes
        ):
            raise InvalidJobError("normalized event exceeds the durable size limit")
        cursor = await connection.execute(
            """
            INSERT INTO webhook_deliveries (
                id, provider_id, delivery_identity, event_name, payload_sha256,
                disposition, ignored_reason, event_schema_version,
                normalized_event_json, semantic_identity, received_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_id, delivery_identity) DO NOTHING RETURNING id
            """,
            (
                delivery_id,
                provider_id,
                delivery_identity,
                event_name,
                payload_sha256,
                normalization.disposition,
                normalization.reason,
                1 if event is not None else None,
                event_json,
                event.semantic_identity if event is not None else None,
                timestamp(received_at),
                timestamp(received_at),
            ),
        )
        inserted = await cursor.fetchone()
        self._connections.fail("after_delivery_insert")
        if inserted is None:
            existing = list(
                await connection.execute_fetchall(
                    "SELECT payload_sha256, linked_job_id FROM webhook_deliveries "
                    "WHERE provider_id=? AND delivery_identity=?",
                    (provider_id, delivery_identity),
                )
            )
            if not existing or existing[0]["payload_sha256"] != payload_sha256:
                signals.append(("delivery_integrity_conflict", None))
                return IngressReceipt(disposition=IngressDisposition.CONFLICT)
            signals.append(("delivery_exact_duplicate", None))
            return IngressReceipt(
                disposition=IngressDisposition.IDEMPOTENT,
                job_id=cast(str | None, existing[0]["linked_job_id"]),
            )

        signals.append(("delivery_accepted", normalization.disposition))
        if normalization.disposition == "ignored" or event is None:
            return IngressReceipt(disposition=IngressDisposition.IGNORED)
        if event.event_type == "installation":
            await self._installations.update(connection, delivery_id, event, received_at)
            return IngressReceipt(disposition=IngressDisposition.ACCEPTED)

        active = list(
            await connection.execute_fetchall(
                "SELECT id FROM queue_jobs WHERE provider_id=? AND job_type=? "
                "AND semantic_identity=? AND state IN ('pending','running','retry_wait')",
                (provider_id, "change_request_validation", event.semantic_identity),
            )
        )
        self._connections.fail("after_semantic_job_lookup")
        if active:
            job_id = cast(str, active[0]["id"])
            signals.append(("semantic_duplicate", None))
        else:
            count = list(
                await connection.execute_fetchall(
                    "SELECT COUNT(*) AS total FROM queue_jobs "
                    "WHERE state IN ('pending','running','retry_wait')"
                )
            )
            self._connections.fail("after_capacity_count")
            if cast(int, count[0]["total"]) >= self._queue.queue_max_active_jobs:
                raise QueueCapacityError("durable queue capacity is exhausted")
            job_id = uuid.uuid4().hex
            cursor = await connection.execute(
                ACTIVE_JOB_INSERT_SQL,
                (
                    job_id,
                    provider_id,
                    "change_request_validation",
                    event.semantic_identity,
                    event_json,
                    self._queue.queue_max_attempts,
                    timestamp(received_at),
                    timestamp(received_at),
                    timestamp(received_at),
                ),
            )
            inserted_job = await cursor.fetchone()
            self._connections.fail("after_job_insert")
            if inserted_job is None:
                canonical = list(
                    await connection.execute_fetchall(
                        "SELECT id FROM queue_jobs WHERE provider_id=? AND job_type=? "
                        "AND semantic_identity=? "
                        "AND state IN ('pending','running','retry_wait')",
                        (provider_id, "change_request_validation", event.semantic_identity),
                    )
                )
                job_id = cast(str, canonical[0]["id"])
                signals.append(("semantic_duplicate", None))
            else:
                signals.append(("job_created", None))
        await connection.execute(
            "UPDATE webhook_deliveries SET linked_job_id=? WHERE id=?", (job_id, delivery_id)
        )
        self._connections.fail("after_delivery_job_link")
        return IngressReceipt(disposition=IngressDisposition.ACCEPTED, job_id=job_id)
