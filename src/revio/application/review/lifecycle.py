"""Phase 6 finding-lifecycle orchestration.

This service runs after the durable authoritative artifact and before/after GitHub review
publication. It performs exact deduplication, deterministic bounded prior selection,
exact-latest-head evidence collection, ambiguity-safe revalidation, per-finding thread
discovery, and ambiguity-safe thread resolution. It never blocks publication of unrelated new
findings and never performs an unsafe repeated external write.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from revio.domain.configuration import EffectiveReviewPolicy, path_is_excluded
from revio.domain.identifiers import ChangeRequestTarget
from revio.domain.lifecycle import (
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
from revio.domain.models import ChangeRequest, DiffCollection
from revio.domain.reviews import UsageDisposition
from revio.errors import ProviderTransientError
from revio.ports.scm import ChangeRequestReadPort, RepositoryContentReadPort

SELECTION_LIMIT = 25
PER_FILE_BYTE_LIMIT = 128 * 1024
AGGREGATE_BYTE_LIMIT = 512 * 1024

REASON_SELECTED = "selected"
REASON_POLICY_EXCLUDED = "policy_excluded"
REASON_SELECTION_LIMIT = "selection_limit"
REASON_NOT_PRIORITIZED = "not_prioritized"
REASON_CONTENT_MISSING = "content_missing"
REASON_CONTENT_OVERSIZED = "content_oversized"
REASON_CONTENT_UNAVAILABLE = "content_unavailable"
REASON_AGGREGATE_LIMIT = "aggregate_limit"
REASON_PATH_INVALID = "path_invalid"
REASON_SUPERSEDED = "superseded"


class LifecycleRepository(Protocol):
    async def upsert_finding(self, record: FindingRecord, now: datetime) -> FindingRecord: ...
    async def get_finding(self, finding_id: str) -> FindingRecord | None: ...
    async def record_occurrence(self, occurrence: FindingOccurrence) -> None: ...
    async def occurrences_for_run(self, run_id: str) -> tuple[FindingOccurrence, ...]: ...
    async def candidate_priors(
        self,
        *,
        provider_id: str,
        installation_id: str,
        repository_id: str,
        external_number: int,
        current_run_id: str,
    ) -> tuple[FindingRecord, ...]: ...
    async def persist_revalidation_run(
        self, run: FindingRevalidationRun
    ) -> FindingRevalidationRun: ...
    async def get_revalidation_run(self, run_id: str) -> FindingRevalidationRun | None: ...
    async def transition_revalidation_run(
        self,
        run_id: str,
        expected: RevalidationRunState,
        target: RevalidationRunState,
        now: datetime,
    ) -> bool: ...
    async def persist_revalidation_result(self, result: FindingRevalidationResult) -> None: ...
    async def revalidation_results(self, run_id: str) -> tuple[FindingRevalidationResult, ...]: ...
    async def reserve_revalidation_call(
        self, identity: RevalidationCallIdentity, now: datetime
    ) -> RevalidationCallRecord: ...
    async def latest_revalidation_call(self, run_id: str) -> RevalidationCallRecord | None: ...
    async def transition_revalidation_call(
        self,
        call_id: str,
        expected: RevalidationCallState,
        target: RevalidationCallState,
        now: datetime,
        *,
        usage: UsageDisposition | None = None,
    ) -> bool: ...
    async def revalidation_usage(self, run_id: str) -> UsageDisposition | None: ...
    async def reserve_thread_link(self, link: ReviewThreadLink) -> ReviewThreadLink: ...
    async def get_thread_link(self, finding_record_id: str) -> ReviewThreadLink | None: ...
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
    ) -> bool: ...
    async def pending_discovery_links(self, run_id: str) -> tuple[ReviewThreadLink, ...]: ...
    async def reserve_resolution_operation(
        self,
        *,
        operation_id: str,
        finding_record_id: str,
        provider_thread_node_id: str,
        head_sha: str,
        now: datetime,
    ) -> ThreadResolutionOperation: ...
    async def get_resolution_operation(
        self, finding_record_id: str
    ) -> ThreadResolutionOperation | None: ...
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
    ) -> bool: ...
    async def mark_finding_resolved(
        self, finding_record_id: str, *, head_sha: str, now: datetime
    ) -> bool: ...
    async def mark_finding_indeterminate(self, finding_record_id: str, now: datetime) -> bool: ...


def changed_path_set(diff: DiffCollection) -> frozenset[str]:
    paths: set[str] = set()
    for item in diff.items:
        for candidate in (item.new_path, item.old_path):
            if candidate:
                paths.add(candidate)
    return frozenset(paths)


def rank_candidates(
    candidates: tuple[FindingRecord, ...],
    *,
    changed_paths: frozenset[str],
    observed_again: frozenset[str],
) -> list[FindingRecord]:
    """Apply the deterministic selection order with a stable finding-id tie-breaker."""

    def key(record: FindingRecord) -> tuple[int, int, str, str]:
        return (
            0 if record.path in changed_paths else 1,
            0 if record.id in observed_again else 1,
            # ISO-8601 timestamps sort lexicographically; invert for descending recency.
            _descending(record.updated_at),
            record.id,
        )

    return sorted(candidates, key=key)


def _descending(value: datetime) -> str:
    # Map a timestamp to a string that sorts most-recent-first under ascending sort.
    micros = int(value.timestamp() * 1_000_000)
    return f"{(10**19 - micros):020d}"


@dataclass(frozen=True)
class RepositoryScope:
    provider_id: str
    installation_id: str
    repository_id: str
    external_number: int

    @property
    def stable_identity(self) -> str:
        return f"{self.provider_id}:{self.installation_id}:{self.repository_id}"

    @property
    def finding_scope(self) -> str:
        return f"{self.stable_identity}:pr:{self.external_number}"


def scope_of(target: ChangeRequestTarget) -> RepositoryScope:
    repository = target.repository
    return RepositoryScope(
        provider_id=str(repository.installation.provider_id),
        installation_id=repository.installation.external_id,
        repository_id=repository.external_id,
        external_number=target.external_number,
    )


@dataclass
class SelectionOutcome:
    selected: tuple[FindingRecord, ...]
    unselected: tuple[tuple[FindingRecord, str], ...] = ()


@dataclass
class ContentOutcome:
    items: list[tuple[FindingRecord, str]] = field(
        default_factory=lambda: list[tuple[FindingRecord, str]]()
    )
    superseded: bool = False


class FindingLifecycleService:
    def __init__(
        self,
        repository: LifecycleRepository,
        reader: ChangeRequestReadPort,
        content: RepositoryContentReadPort,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._content = content

    async def select_priors(
        self,
        *,
        run_id: str,
        current: ChangeRequest,
        diff: DiffCollection,
        policy: EffectiveReviewPolicy,
        now: datetime,
    ) -> SelectionOutcome:
        """Deterministically select at most 25 prior open findings and persist the decisions.

        Observed-again priors are detected from the current run's durable exact-duplicate
        occurrences, so deduplication must run before selection.
        """
        scope = scope_of(current.target)
        candidates = await self._repository.candidate_priors(
            provider_id=scope.provider_id,
            installation_id=scope.installation_id,
            repository_id=scope.repository_id,
            external_number=scope.external_number,
            current_run_id=run_id,
        )
        occurrences = await self._repository.occurrences_for_run(run_id)
        observed_again = frozenset(
            occ.finding_record_id
            for occ in occurrences
            if occ.disposition == OccurrenceDisposition.EXACT_DUPLICATE
        )
        changed = changed_path_set(diff)

        excluded: list[FindingRecord] = []
        eligible: list[FindingRecord] = []
        for record in candidates:
            if path_is_excluded_safe(record.path, policy.exclusions):
                excluded.append(record)
            else:
                eligible.append(record)
        ranked = rank_candidates(
            tuple(eligible), changed_paths=changed, observed_again=observed_again
        )
        selected = tuple(ranked[:SELECTION_LIMIT])
        overflow = ranked[SELECTION_LIMIT:]
        unselected: list[tuple[FindingRecord, str]] = [
            (record, REASON_POLICY_EXCLUDED) for record in excluded
        ]
        unselected.extend((record, REASON_SELECTION_LIMIT) for record in overflow)

        existing_run = await self._repository.get_revalidation_run(run_id)
        if existing_run is None:
            selected_ids = {record.id for record in selected}
            for record in candidates:
                disposition = (
                    OccurrenceDisposition.SELECTED_PRIOR
                    if record.id in selected_ids
                    else OccurrenceDisposition.UNSELECTED_PRIOR
                )
                await self._repository.record_occurrence(
                    FindingOccurrence(
                        finding_record_id=record.id,
                        review_run_id=run_id,
                        artifact_ordinal=0,
                        observed_head_sha=current.head_sha,
                        path=record.path,
                        line=None,
                        disposition=disposition,
                        publication_eligible=False,
                        created_at=now,
                    )
                )
            await self._repository.persist_revalidation_run(
                FindingRevalidationRun(
                    review_run_id=run_id,
                    validated_head_sha=current.head_sha,
                    selection_limit=SELECTION_LIMIT,
                    selected_count=len(selected),
                    unselected_count=len(unselected),
                    policy_digest=policy.digest,
                    state=RevalidationRunState.SELECTED,
                    created_at=now,
                    updated_at=now,
                )
            )
        # Persist durable uncertain outcomes for every unselected prior.
        for record, reason in unselected:
            await self._persist_uncertain(run_id, record, reason, None, now)
        return SelectionOutcome(selected=selected, unselected=tuple(unselected))

    async def collect_content(
        self,
        *,
        run_id: str,
        target: ChangeRequestTarget,
        validated_head: str,
        selected: tuple[FindingRecord, ...],
        policy: EffectiveReviewPolicy,
        now: datetime,
    ) -> ContentOutcome:
        """Fetch bounded current-file content at the exact latest head for selected findings.

        Re-fetches the change request first and supersedes the run when the head has moved.
        Every unavailable, oversized, excluded, or limit-exhausted outcome becomes a durable
        uncertain result; a transient read is allowed to propagate for a bounded job retry
        because no revalidation call or lifecycle external write has begun yet.
        """
        refreshed = await self._reader.get_change_request(target)
        if refreshed.head_sha != validated_head:
            return ContentOutcome(superseded=True)

        outcome = ContentOutcome()
        aggregate = 0
        for record in selected:
            if path_is_excluded_safe(record.path, policy.exclusions):
                await self._persist_uncertain(run_id, record, REASON_POLICY_EXCLUDED, None, now)
                continue
            try:
                content = await self._content.get_file(target, record.path, validated_head)
            except ProviderTransientError:
                raise
            except Exception:
                await self._persist_uncertain(
                    run_id, record, REASON_CONTENT_UNAVAILABLE, validated_head, now
                )
                continue
            if content is None:
                await self._persist_uncertain(
                    run_id, record, REASON_CONTENT_MISSING, validated_head, now
                )
                continue
            encoded_length = len(content.encode("utf-8"))
            if encoded_length > PER_FILE_BYTE_LIMIT:
                await self._persist_uncertain(
                    run_id, record, REASON_CONTENT_OVERSIZED, validated_head, now
                )
                continue
            if aggregate + encoded_length > AGGREGATE_BYTE_LIMIT:
                await self._persist_uncertain(
                    run_id, record, REASON_AGGREGATE_LIMIT, validated_head, now
                )
                continue
            aggregate += encoded_length
            outcome.items.append((record, content))
        return outcome

    async def _persist_uncertain(
        self,
        run_id: str,
        record: FindingRecord,
        reason: str,
        fetched_ref: str | None,
        now: datetime,
    ) -> None:
        await self._repository.persist_revalidation_result(
            FindingRevalidationResult(
                review_run_id=run_id,
                finding_record_id=record.id,
                outcome=RevalidationOutcome.UNCERTAIN,
                reason_code=reason,
                fetched_ref=fetched_ref,
                current_path=None,
                created_at=now,
                updated_at=now,
            )
        )


def path_is_excluded_safe(path: str, exclusions: tuple[str, ...]) -> bool:
    try:
        return path_is_excluded(path, exclusions)
    except ValueError:
        return True
