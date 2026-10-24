"""Durable Phase 6 finding records, occurrences, revalidation, links, and resolution."""

from datetime import datetime

import aiosqlite

from revio.adapters.persistence.sqlite.connection import SQLiteConnectionPolicy
from revio.adapters.persistence.sqlite.values import datetime_value, timestamp
from revio.domain.lifecycle import (
    FindingLifecycleState,
    FindingOccurrence,
    FindingRecord,
    FindingRevalidationResult,
    FindingRevalidationRun,
    OccurrenceDisposition,
    RevalidationCallIdentity,
    RevalidationCallRecord,
    RevalidationCallState,
    RevalidationOutcome,
    RevalidationRunState,
    ReviewThreadLink,
    ThreadLinkState,
    ThreadResolutionOperation,
    ThreadResolutionState,
)
from revio.domain.models import TokenUsage
from revio.domain.reviews import UsageDisposition
from revio.errors import PersistenceIntegrityError

_FINDING_UNIQUE = (
    "provider_id=? AND installation_id=? AND repository_id=? "
    "AND external_number=? AND primary_fingerprint=?"
)


def _dt(value: str | None) -> datetime:
    parsed = datetime_value(value)
    if parsed is None:
        raise PersistenceIntegrityError("lifecycle timestamp is missing")
    return parsed


def _finding(row: aiosqlite.Row) -> FindingRecord:
    return FindingRecord(
        id=row["id"],
        provider_id=row["provider_id"],
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        external_number=row["external_number"],
        fingerprint_version=row["fingerprint_version"],
        primary_fingerprint=row["primary_fingerprint"],
        secondary_fingerprint=row["secondary_fingerprint"],
        path=row["path"],
        category=row["category"],
        rule=row["rule"],
        symbol=row["symbol"],
        evidence=row["evidence"],
        evidence_digest=row["evidence_digest"],
        state=FindingLifecycleState(row["state"]),
        first_seen_run_id=row["first_seen_run_id"],
        last_seen_run_id=row["last_seen_run_id"],
        first_seen_head_sha=row["first_seen_head_sha"],
        last_seen_head_sha=row["last_seen_head_sha"],
        resolved_head_sha=row["resolved_head_sha"],
        resolved_at=datetime_value(row["resolved_at"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _occurrence(row: aiosqlite.Row) -> FindingOccurrence:
    return FindingOccurrence(
        finding_record_id=row["finding_record_id"],
        review_run_id=row["review_run_id"],
        artifact_ordinal=row["artifact_ordinal"],
        observed_head_sha=row["observed_head_sha"],
        path=row["path"],
        line=row["line"],
        disposition=OccurrenceDisposition(row["disposition"]),
        publication_eligible=bool(row["publication_eligible"]),
        comment_linked=bool(row["comment_linked"]),
        created_at=_dt(row["created_at"]),
    )


def _link(row: aiosqlite.Row) -> ReviewThreadLink:
    return ReviewThreadLink(
        finding_record_id=row["finding_record_id"],
        review_run_id=row["review_run_id"],
        provider_review_id=row["provider_review_id"],
        provider_thread_node_id=row["provider_thread_node_id"],
        provider_comment_id=row["provider_comment_id"],
        marker_digest=row["marker_digest"],
        marker_version=row["marker_version"],
        state=ThreadLinkState(row["state"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _reval_result(row: aiosqlite.Row) -> FindingRevalidationResult:
    return FindingRevalidationResult(
        review_run_id=row["review_run_id"],
        finding_record_id=row["finding_record_id"],
        outcome=RevalidationOutcome(row["outcome"]),
        reason_code=row["reason_code"],
        fetched_ref=row["fetched_ref"],
        current_path=row["current_path"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _resolution(row: aiosqlite.Row) -> ThreadResolutionOperation:
    return ThreadResolutionOperation(
        id=row["id"],
        finding_record_id=row["finding_record_id"],
        provider_thread_node_id=row["provider_thread_node_id"],
        validated_head_sha=row["validated_head_sha"],
        state=ThreadResolutionState(row["state"]),
        attempt_count=row["attempt_count"],
        provider_observed_state=row["provider_observed_state"],
        terminal_reason=row["terminal_reason"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def _reval_call(row: aiosqlite.Row) -> RevalidationCallRecord:
    return RevalidationCallRecord(
        identity=RevalidationCallIdentity(
            id=row["id"],
            review_run_id=row["review_run_id"],
            batch_ordinal=row["batch_ordinal"],
            provider_id=row["provider_id"],
            model_profile_id=row["model_profile_id"],
            model_profile_version=row["model_profile_version"],
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            estimated_input_tokens=row["estimated_input_tokens"],
        ),
        state=RevalidationCallState(row["state"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


class SQLiteFindingLifecycleRepository:
    def __init__(self, connections: SQLiteConnectionPolicy) -> None:
        self._connections = connections

    # ---- Finding records and occurrences -------------------------------------------------

    async def upsert_finding(self, record: FindingRecord, now: datetime) -> FindingRecord:
        """Idempotently insert a finding or update safe last-seen metadata of an existing one."""
        stamp = timestamp(now)
        scope = (
            record.provider_id,
            record.installation_id,
            record.repository_id,
            record.external_number,
            record.primary_fingerprint,
        )

        async def operation(connection: aiosqlite.Connection) -> FindingRecord:
            await connection.execute(
                "INSERT INTO finding_records (id, provider_id, installation_id, repository_id, "
                "external_number, fingerprint_version, primary_fingerprint, "
                "secondary_fingerprint, path, category, rule, symbol, evidence, evidence_digest, "
                "state, first_seen_run_id, last_seen_run_id, first_seen_head_sha, "
                "last_seen_head_sha, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider_id, installation_id, repository_id, external_number, "
                "primary_fingerprint) DO NOTHING",
                (
                    record.id,
                    record.provider_id,
                    record.installation_id,
                    record.repository_id,
                    record.external_number,
                    record.fingerprint_version,
                    record.primary_fingerprint,
                    record.secondary_fingerprint,
                    record.path,
                    record.category,
                    record.rule,
                    record.symbol,
                    record.evidence,
                    record.evidence_digest,
                    record.first_seen_run_id,
                    record.last_seen_run_id,
                    record.first_seen_head_sha,
                    record.last_seen_head_sha,
                    stamp,
                    stamp,
                ),
            )
            row = await (
                await connection.execute(
                    f"SELECT * FROM finding_records WHERE {_FINDING_UNIQUE}", scope
                )
            ).fetchone()
            if row is None or row["id"] != record.id:
                raise PersistenceIntegrityError("finding record identity conflict")
            if row["first_seen_run_id"] != record.first_seen_run_id:
                # Pre-existing record: advance only safe last-seen metadata, never lifecycle state.
                await connection.execute(
                    "UPDATE finding_records SET last_seen_run_id=?, last_seen_head_sha=?, "
                    "updated_at=? WHERE id=?",
                    (record.last_seen_run_id, record.last_seen_head_sha, stamp, record.id),
                )
                row = await (
                    await connection.execute(
                        "SELECT * FROM finding_records WHERE id=?", (record.id,)
                    )
                ).fetchone()
                assert row is not None
            return _finding(row)

        return await self._connections.write(operation)

    async def get_finding(self, finding_id: str) -> FindingRecord | None:
        async def operation(connection: aiosqlite.Connection) -> FindingRecord | None:
            row = await (
                await connection.execute("SELECT * FROM finding_records WHERE id=?", (finding_id,))
            ).fetchone()
            return _finding(row) if row is not None else None

        return await self._connections.read(operation)

    async def record_occurrence(self, occurrence: FindingOccurrence) -> None:
        async def operation(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT INTO finding_occurrences (finding_record_id, review_run_id, "
                "artifact_ordinal, observed_head_sha, path, line, disposition, "
                "publication_eligible, comment_linked, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(finding_record_id, review_run_id) DO NOTHING",
                (
                    occurrence.finding_record_id,
                    occurrence.review_run_id,
                    occurrence.artifact_ordinal,
                    occurrence.observed_head_sha,
                    occurrence.path,
                    occurrence.line,
                    str(occurrence.disposition),
                    int(occurrence.publication_eligible),
                    int(occurrence.comment_linked),
                    timestamp(occurrence.created_at),
                ),
            )

        await self._connections.write(operation)

    async def occurrences_for_run(self, run_id: str) -> tuple[FindingOccurrence, ...]:
        async def operation(connection: aiosqlite.Connection) -> tuple[FindingOccurrence, ...]:
            rows = await (
                await connection.execute(
                    "SELECT * FROM finding_occurrences WHERE review_run_id=? "
                    "ORDER BY artifact_ordinal, finding_record_id",
                    (run_id,),
                )
            ).fetchall()
            return tuple(_occurrence(row) for row in rows)

        return await self._connections.read(operation)

    async def candidate_priors(
        self,
        *,
        provider_id: str,
        installation_id: str,
        repository_id: str,
        external_number: int,
        current_run_id: str,
    ) -> tuple[FindingRecord, ...]:
        """Return open findings for the scope first observed before the current run."""

        async def operation(connection: aiosqlite.Connection) -> tuple[FindingRecord, ...]:
            rows = await (
                await connection.execute(
                    "SELECT * FROM finding_records WHERE provider_id=? AND installation_id=? "
                    "AND repository_id=? AND external_number=? AND state='open' "
                    "AND first_seen_run_id<>? ORDER BY id",
                    (
                        provider_id,
                        installation_id,
                        repository_id,
                        external_number,
                        current_run_id,
                    ),
                )
            ).fetchall()
            return tuple(_finding(row) for row in rows)

        return await self._connections.read(operation)

    # ---- Revalidation run and results ----------------------------------------------------

    async def persist_revalidation_run(self, run: FindingRevalidationRun) -> FindingRevalidationRun:
        async def operation(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT INTO finding_revalidation_runs (review_run_id, validated_head_sha, "
                "selection_limit, selected_count, unselected_count, policy_digest, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(review_run_id) DO NOTHING",
                (
                    run.review_run_id,
                    run.validated_head_sha,
                    run.selection_limit,
                    run.selected_count,
                    run.unselected_count,
                    run.policy_digest,
                    str(run.state),
                    timestamp(run.created_at),
                    timestamp(run.updated_at),
                ),
            )

        await self._connections.write(operation)
        existing = await self.get_revalidation_run(run.review_run_id)
        if existing is None:
            raise PersistenceIntegrityError("revalidation run could not be persisted")
        return existing

    async def get_revalidation_run(self, run_id: str) -> FindingRevalidationRun | None:
        async def operation(connection: aiosqlite.Connection) -> FindingRevalidationRun | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM finding_revalidation_runs WHERE review_run_id=?", (run_id,)
                )
            ).fetchone()
            if row is None:
                return None
            return FindingRevalidationRun(
                review_run_id=row["review_run_id"],
                validated_head_sha=row["validated_head_sha"],
                selection_limit=row["selection_limit"],
                selected_count=row["selected_count"],
                unselected_count=row["unselected_count"],
                policy_digest=row["policy_digest"],
                state=RevalidationRunState(row["state"]),
                created_at=_dt(row["created_at"]),
                updated_at=_dt(row["updated_at"]),
            )

        return await self._connections.read(operation)

    async def transition_revalidation_run(
        self,
        run_id: str,
        expected: RevalidationRunState,
        target: RevalidationRunState,
        now: datetime,
    ) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE finding_revalidation_runs SET state=?, updated_at=? "
                "WHERE review_run_id=? AND state=?",
                (str(target), timestamp(now), run_id, str(expected)),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def persist_revalidation_result(self, result: FindingRevalidationResult) -> None:
        async def operation(connection: aiosqlite.Connection) -> None:
            await connection.execute(
                "INSERT INTO finding_revalidation_results (review_run_id, finding_record_id, "
                "outcome, reason_code, fetched_ref, current_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(review_run_id, finding_record_id) DO NOTHING",
                (
                    result.review_run_id,
                    result.finding_record_id,
                    str(result.outcome),
                    result.reason_code,
                    result.fetched_ref,
                    result.current_path,
                    timestamp(result.created_at),
                    timestamp(result.updated_at),
                ),
            )

        await self._connections.write(operation)

    async def revalidation_results(self, run_id: str) -> tuple[FindingRevalidationResult, ...]:
        async def operation(
            connection: aiosqlite.Connection,
        ) -> tuple[FindingRevalidationResult, ...]:
            rows = await (
                await connection.execute(
                    "SELECT * FROM finding_revalidation_results WHERE review_run_id=? "
                    "ORDER BY finding_record_id",
                    (run_id,),
                )
            ).fetchall()
            return tuple(_reval_result(row) for row in rows)

        return await self._connections.read(operation)

    # ---- Revalidation provider call ------------------------------------------------------

    async def reserve_revalidation_call(
        self, identity: RevalidationCallIdentity, now: datetime
    ) -> RevalidationCallRecord:
        stamp = timestamp(now)

        async def operation(connection: aiosqlite.Connection) -> RevalidationCallRecord:
            await connection.execute(
                "INSERT INTO revalidation_calls (id, review_run_id, batch_ordinal, provider_id, "
                "model_profile_id, model_profile_version, prompt_version, schema_version, "
                "estimated_input_tokens, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?) "
                "ON CONFLICT(review_run_id, batch_ordinal) DO NOTHING",
                (
                    identity.id,
                    identity.review_run_id,
                    identity.batch_ordinal,
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
                    "SELECT * FROM revalidation_calls WHERE review_run_id=? AND batch_ordinal=?",
                    (identity.review_run_id, identity.batch_ordinal),
                )
            ).fetchone()
            if row is None or row["id"] != identity.id:
                raise PersistenceIntegrityError("revalidation call identity conflict")
            return _reval_call(row)

        return await self._connections.write(operation)

    async def latest_revalidation_call(self, run_id: str) -> RevalidationCallRecord | None:
        async def operation(connection: aiosqlite.Connection) -> RevalidationCallRecord | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM revalidation_calls WHERE review_run_id=? "
                    "ORDER BY batch_ordinal DESC LIMIT 1",
                    (run_id,),
                )
            ).fetchone()
            return _reval_call(row) if row is not None else None

        return await self._connections.read(operation)

    async def transition_revalidation_call(
        self,
        call_id: str,
        expected: RevalidationCallState,
        target: RevalidationCallState,
        now: datetime,
        *,
        usage: UsageDisposition | None = None,
    ) -> bool:
        allowed = {
            RevalidationCallState.RESERVED: {RevalidationCallState.ATTEMPT_STARTED},
            RevalidationCallState.ATTEMPT_STARTED: {
                RevalidationCallState.RESPONSE_OBSERVED,
                RevalidationCallState.AMBIGUOUS,
                RevalidationCallState.RETRYABLE_REJECTED,
                RevalidationCallState.TERMINAL_REJECTED,
            },
            RevalidationCallState.RESPONSE_OBSERVED: {RevalidationCallState.COMPLETED},
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal revalidation-call state transition")

        async def operation(connection: aiosqlite.Connection) -> bool:
            values: dict[str, object] = {}
            if usage is not None:
                known = usage.usage if usage.status == "known" else None
                values = {
                    "usage_status": usage.status,
                    "uncached_input_tokens": known.uncached_input_tokens if known else None,
                    "cached_input_tokens": known.cached_input_tokens if known else None,
                    "cache_creation_tokens": known.cache_creation_tokens if known else None,
                    "output_tokens": known.output_tokens if known else None,
                }
            if values:
                cursor = await connection.execute(
                    "UPDATE revalidation_calls SET state=?, updated_at=?, usage_status=?, "
                    "uncached_input_tokens=?, cached_input_tokens=?, cache_creation_tokens=?, "
                    "output_tokens=? WHERE id=? AND state=?",
                    (
                        str(target),
                        timestamp(now),
                        values["usage_status"],
                        values["uncached_input_tokens"],
                        values["cached_input_tokens"],
                        values["cache_creation_tokens"],
                        values["output_tokens"],
                        call_id,
                        str(expected),
                    ),
                )
            else:
                cursor = await connection.execute(
                    "UPDATE revalidation_calls SET state=?, updated_at=? WHERE id=? AND state=?",
                    (str(target), timestamp(now), call_id, str(expected)),
                )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def revalidation_usage(self, run_id: str) -> UsageDisposition | None:
        async def operation(connection: aiosqlite.Connection) -> UsageDisposition | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM revalidation_calls WHERE review_run_id=? AND usage_status "
                    "IS NOT NULL ORDER BY batch_ordinal DESC LIMIT 1",
                    (run_id,),
                )
            ).fetchone()
            if row is None:
                return None
            if row["usage_status"] == "unknown":
                return UsageDisposition(status="unknown")
            return UsageDisposition(
                status="known",
                usage=TokenUsage(
                    uncached_input_tokens=row["uncached_input_tokens"],
                    cached_input_tokens=row["cached_input_tokens"],
                    cache_creation_tokens=row["cache_creation_tokens"],
                    output_tokens=row["output_tokens"],
                ),
            )

        return await self._connections.read(operation)

    # ---- Thread links --------------------------------------------------------------------

    async def reserve_thread_link(self, link: ReviewThreadLink) -> ReviewThreadLink:
        async def operation(connection: aiosqlite.Connection) -> ReviewThreadLink:
            await connection.execute(
                "INSERT INTO review_thread_links (finding_record_id, review_run_id, "
                "provider_review_id, provider_thread_node_id, provider_comment_id, marker_digest, "
                "marker_version, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(finding_record_id) DO NOTHING",
                (
                    link.finding_record_id,
                    link.review_run_id,
                    link.provider_review_id,
                    link.provider_thread_node_id,
                    link.provider_comment_id,
                    link.marker_digest,
                    link.marker_version,
                    str(link.state),
                    timestamp(link.created_at),
                    timestamp(link.updated_at),
                ),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM review_thread_links WHERE finding_record_id=?",
                    (link.finding_record_id,),
                )
            ).fetchone()
            if row is None or row["marker_digest"] != link.marker_digest:
                raise PersistenceIntegrityError("thread link identity conflict")
            return _link(row)

        return await self._connections.write(operation)

    async def get_thread_link(self, finding_record_id: str) -> ReviewThreadLink | None:
        async def operation(connection: aiosqlite.Connection) -> ReviewThreadLink | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM review_thread_links WHERE finding_record_id=?",
                    (finding_record_id,),
                )
            ).fetchone()
            return _link(row) if row is not None else None

        return await self._connections.read(operation)

    async def transition_thread_link(
        self,
        finding_record_id: str,
        expected: ThreadLinkState,
        target: ThreadLinkState,
        now: datetime,
        *,
        provider_thread_node_id: str | None = None,
        provider_review_id: str | None = None,
        provider_comment_id: str | None = None,
    ) -> bool:
        allowed = {
            ThreadLinkState.PENDING_DISCOVERY: {
                ThreadLinkState.LINKED,
                ThreadLinkState.NOT_THREADED,
                ThreadLinkState.DISCOVERY_INDETERMINATE,
            },
            ThreadLinkState.DISCOVERY_INDETERMINATE: {
                ThreadLinkState.LINKED,
                ThreadLinkState.NOT_THREADED,
                ThreadLinkState.DISCOVERY_INDETERMINATE,
            },
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal thread-link state transition")
        if target == ThreadLinkState.LINKED and provider_thread_node_id is None:
            raise PersistenceIntegrityError("linked thread requires a provider thread node id")

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE review_thread_links SET state=?, "
                "provider_thread_node_id=COALESCE(?, provider_thread_node_id), "
                "provider_review_id=COALESCE(?, provider_review_id), "
                "provider_comment_id=COALESCE(?, provider_comment_id), updated_at=? "
                "WHERE finding_record_id=? AND state=?",
                (
                    str(target),
                    provider_thread_node_id,
                    provider_review_id,
                    provider_comment_id,
                    timestamp(now),
                    finding_record_id,
                    str(expected),
                ),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def pending_discovery_links(self, run_id: str) -> tuple[ReviewThreadLink, ...]:
        async def operation(connection: aiosqlite.Connection) -> tuple[ReviewThreadLink, ...]:
            rows = await (
                await connection.execute(
                    "SELECT * FROM review_thread_links WHERE review_run_id=? "
                    "AND state IN ('pending_discovery', 'discovery_indeterminate') "
                    "ORDER BY finding_record_id",
                    (run_id,),
                )
            ).fetchall()
            return tuple(_link(row) for row in rows)

        return await self._connections.read(operation)

    # ---- Thread resolution operations ----------------------------------------------------

    async def reserve_resolution_operation(
        self,
        *,
        operation_id: str,
        finding_record_id: str,
        provider_thread_node_id: str,
        head_sha: str,
        now: datetime,
    ) -> ThreadResolutionOperation:
        stamp = timestamp(now)

        async def operation(connection: aiosqlite.Connection) -> ThreadResolutionOperation:
            await connection.execute(
                "INSERT INTO thread_resolution_operations (id, finding_record_id, "
                "provider_thread_node_id, validated_head_sha, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'reserved_unattempted', ?, ?) "
                "ON CONFLICT(finding_record_id) DO NOTHING",
                (operation_id, finding_record_id, provider_thread_node_id, head_sha, stamp, stamp),
            )
            row = await (
                await connection.execute(
                    "SELECT * FROM thread_resolution_operations WHERE finding_record_id=?",
                    (finding_record_id,),
                )
            ).fetchone()
            if row is None or row["id"] != operation_id:
                raise PersistenceIntegrityError("resolution operation identity conflict")
            if row["provider_thread_node_id"] != provider_thread_node_id:
                raise PersistenceIntegrityError("resolution operation thread identity conflict")
            return _resolution(row)

        return await self._connections.write(operation)

    async def get_resolution_operation(
        self, finding_record_id: str
    ) -> ThreadResolutionOperation | None:
        async def operation(
            connection: aiosqlite.Connection,
        ) -> ThreadResolutionOperation | None:
            row = await (
                await connection.execute(
                    "SELECT * FROM thread_resolution_operations WHERE finding_record_id=?",
                    (finding_record_id,),
                )
            ).fetchone()
            return _resolution(row) if row is not None else None

        return await self._connections.read(operation)

    async def transition_resolution_operation(
        self,
        operation_id: str,
        expected: ThreadResolutionState,
        target: ThreadResolutionState,
        now: datetime,
        *,
        provider_observed_state: str | None = None,
        terminal_reason: str | None = None,
        increment_attempt: bool = False,
    ) -> bool:
        allowed = {
            ThreadResolutionState.RESERVED_UNATTEMPTED: {
                ThreadResolutionState.ATTEMPT_STARTED,
                ThreadResolutionState.RECONCILED,
                ThreadResolutionState.INTEGRITY_FAILED,
            },
            ThreadResolutionState.ATTEMPT_STARTED: {
                ThreadResolutionState.KNOWN_REJECTED,
                ThreadResolutionState.AMBIGUOUS,
                ThreadResolutionState.COMPLETED,
            },
            ThreadResolutionState.AMBIGUOUS: {
                ThreadResolutionState.RECONCILED,
                ThreadResolutionState.INTEGRITY_FAILED,
            },
        }
        if target not in allowed.get(expected, set()):
            raise PersistenceIntegrityError("illegal resolution-operation state transition")

        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE thread_resolution_operations SET state=?, "
                "provider_observed_state=COALESCE(?, provider_observed_state), "
                "terminal_reason=COALESCE(?, terminal_reason), "
                "attempt_count=attempt_count+?, updated_at=? WHERE id=? AND state=?",
                (
                    str(target),
                    provider_observed_state,
                    terminal_reason,
                    int(increment_attempt),
                    timestamp(now),
                    operation_id,
                    str(expected),
                ),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def mark_finding_resolved(
        self, finding_record_id: str, *, head_sha: str, now: datetime
    ) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE finding_records SET state='resolved', resolved_head_sha=?, resolved_at=?, "
                "updated_at=? WHERE id=? AND state='open'",
                (head_sha, timestamp(now), timestamp(now), finding_record_id),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)

    async def mark_finding_indeterminate(self, finding_record_id: str, now: datetime) -> bool:
        async def operation(connection: aiosqlite.Connection) -> bool:
            cursor = await connection.execute(
                "UPDATE finding_records SET state='resolution_indeterminate', updated_at=? "
                "WHERE id=? AND state='open'",
                (timestamp(now), finding_record_id),
            )
            return cursor.rowcount == 1

        return await self._connections.write(operation)
