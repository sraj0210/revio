"""Phase 6 finding-lifecycle orchestration.

This service runs after the durable authoritative artifact and before/after GitHub review
publication. It performs exact deduplication, deterministic bounded prior selection,
exact-latest-head evidence collection, ambiguity-safe revalidation, per-finding thread
discovery, and ambiguity-safe thread resolution. It never blocks publication of unrelated new
findings and never performs an unsafe repeated external write.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from venaroq.application.review.markers import (
    FINDING_MARKER_VERSION,
    finding_marker,
    finding_marker_token,
)
from venaroq.domain.capabilities import ResolvedModelProfile
from venaroq.domain.configuration import EffectiveReviewPolicy, path_is_excluded
from venaroq.domain.finding_identity import (
    FINGERPRINT_VERSION,
    finding_identity_is_valid,
    finding_record_id,
    normalize_category,
    normalize_multiline,
    normalize_path,
    normalize_rule,
    normalize_symbol,
    primary_fingerprint_v1,
    secondary_fingerprint_v1,
)
from venaroq.domain.identifiers import ChangeRequestTarget
from venaroq.domain.lifecycle import (
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
from venaroq.domain.models import ChangeRequest, DiffCollection, Finding, TokenUsage
from venaroq.domain.revalidation import FindingRevalidationRequest, RevalidationRequestItem
from venaroq.domain.reviews import NormalizedReviewArtifact, UsageDisposition
from venaroq.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    PersistenceIntegrityError,
    ProviderCallAmbiguousError,
    ProviderCallObservedInvalidResponseError,
    ProviderCallObservedTerminalError,
    ProviderCallRejectedError,
    ProviderCallSafeRetryError,
    ProviderCallTerminalError,
    ProviderTransientError,
    ProviderWriteAmbiguousError,
    ProviderWriteRejectedError,
)
from venaroq.ports.ai import AIFindingRevalidator
from venaroq.ports.scm import (
    ChangeRequestReadPort,
    RepositoryContentReadPort,
    ThreadReaderPort,
    ThreadResolverPort,
)

SELECTION_LIMIT = 25
PER_FILE_BYTE_LIMIT = 128 * 1024
AGGREGATE_BYTE_LIMIT = 512 * 1024
# The revalidation contract version is provider-neutral and owned by the application layer, kept
# independent of any adapter to avoid an application->adapter dependency; every provider adapter
# builds requests against the same shared schema and must stay in sync with these constants.
REVALIDATION_PROMPT_VERSION = "revalidation-v1"
REVALIDATION_SCHEMA_VERSION = "revalidation-v1"

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
REASON_REVALIDATION_LIMIT = "revalidation_limit"
REASON_REVALIDATION_AMBIGUOUS = "revalidation_ambiguous"
REASON_REVALIDATION_MALFORMED = "revalidation_malformed"
REASON_REVALIDATION_REJECTED = "revalidation_rejected"
REASON_REVALIDATION_UNAVAILABLE = "revalidation_unavailable"
REASON_RESPONSE_RECOVERY = "response_recovery_unavailable"


def _id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"venaroq:{namespace}:{value}"))


def _estimated_tokens(payload: dict[str, object]) -> int:
    value = payload.get("_venaroq_estimated_input_tokens", 0)
    if not isinstance(value, int) or value < 0:
        raise RuntimeError("revalidation preflight returned an invalid token estimate")
    return value


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


@dataclass(frozen=True)
class PublicationProjection:
    """The durable set of only-new findings eligible for publication this run."""

    new: tuple[tuple[Finding, str], ...] = ()

    @property
    def has_new(self) -> bool:
        return bool(self.new)

    @property
    def inline(self) -> tuple[tuple[Finding, str], ...]:
        return tuple(
            (finding, record_id) for finding, record_id in self.new if finding.inline_eligible
        )

    @property
    def summary_pairs(self) -> tuple[tuple[Finding, str], ...]:
        return tuple(
            (finding, record_id) for finding, record_id in self.new if not finding.inline_eligible
        )

    @property
    def summary_findings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding, _ in self.new if not finding.inline_eligible)


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
        *,
        revalidator: AIFindingRevalidator | None = None,
        thread_reader: ThreadReaderPort | None = None,
        thread_resolver: ThreadResolverPort | None = None,
        marker_key: bytes | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._content = content
        self._revalidator = revalidator
        self._thread_reader = thread_reader
        self._thread_resolver = thread_resolver
        self._marker_key = marker_key

    def finding_marker(self, finding_record_id: str) -> str:
        """Render the hidden per-finding marker comment for publication."""
        if self._marker_key is None:
            raise RuntimeError("marker key is required to render a finding marker")
        return finding_marker(finding_marker_token(self._marker_key, finding_record_id))

    async def register_pending_link(
        self,
        *,
        run_id: str,
        finding_record_id: str,
        provider_review_id: str | None,
        now: datetime,
    ) -> None:
        if self._marker_key is None:
            raise RuntimeError("marker key is required to register a thread link")
        token = finding_marker_token(self._marker_key, finding_record_id)
        await self._repository.reserve_thread_link(
            ReviewThreadLink(
                finding_record_id=finding_record_id,
                review_run_id=run_id,
                provider_review_id=provider_review_id,
                marker_digest=token,
                marker_version=FINDING_MARKER_VERSION,
                state=ThreadLinkState.PENDING_DISCOVERY,
                created_at=now,
                updated_at=now,
            )
        )

    async def register_not_threaded(
        self, *, run_id: str, finding_record_id: str, now: datetime
    ) -> None:
        if self._marker_key is None:
            raise RuntimeError("marker key is required to register a thread link")
        token = finding_marker_token(self._marker_key, finding_record_id)
        await self._repository.reserve_thread_link(
            ReviewThreadLink(
                finding_record_id=finding_record_id,
                review_run_id=run_id,
                marker_digest=token,
                marker_version=FINDING_MARKER_VERSION,
                state=ThreadLinkState.NOT_THREADED,
                created_at=now,
                updated_at=now,
            )
        )

    async def discover_and_link(
        self, *, run_id: str, target: ChangeRequestTarget, now: datetime
    ) -> None:
        """Discover exactly one thread per pending per-finding marker and persist the link.

        Zero, multiple, incomplete, or conflicting discovery remains discovery_indeterminate;
        it never guesses. A transient read propagates for a bounded job retry.
        """
        if self._thread_reader is None or self._marker_key is None:
            return
        for link in await self._repository.pending_discovery_links(run_id):
            marker = finding_marker(link.marker_digest)
            result = await self._thread_reader.discover_thread(target, marker=marker)
            if result.match_count == 1 and result.collection_complete:
                await self._repository.transition_thread_link(
                    link.finding_record_id,
                    link.state,
                    ThreadLinkState.LINKED,
                    now,
                    provider_thread_node_id=result.thread_node_ids[0],
                    provider_review_id=result.review_ids[0] if result.review_ids else None,
                    provider_comment_id=result.comment_ids[0] if result.comment_ids else None,
                )
            elif link.state != ThreadLinkState.DISCOVERY_INDETERMINATE:
                await self._repository.transition_thread_link(
                    link.finding_record_id,
                    link.state,
                    ThreadLinkState.DISCOVERY_INDETERMINATE,
                    now,
                )

    async def resolve_threads(
        self,
        *,
        run_id: str,
        current: ChangeRequest,
        validated_head: str,
        now: datetime,
    ) -> None:
        """Resolve only linked threads of durable fixed findings at the exact latest head.

        Re-fetches the change request first; a newer head supersedes lifecycle writes and
        leaves every finding open. Each operation performs at most one mutation, verified by
        reread; ambiguous outcomes reconcile read-only and never repeat the mutation.
        """
        if self._thread_resolver is None:
            return
        fixed = [
            result
            for result in await self._repository.revalidation_results(run_id)
            if result.outcome == RevalidationOutcome.FIXED and result.fetched_ref == validated_head
        ]
        if not fixed:
            return
        refreshed = await self._reader.get_change_request(current.target)
        if refreshed.head_sha != validated_head:
            return
        for result in fixed:
            link = await self._repository.get_thread_link(result.finding_record_id)
            if (
                link is None
                or link.state != ThreadLinkState.LINKED
                or link.provider_thread_node_id is None
            ):
                continue
            finding = await self._repository.get_finding(result.finding_record_id)
            if finding is None or finding.state != FindingLifecycleState.OPEN:
                continue
            await self._resolve_one(
                run_id=run_id,
                target=current.target,
                finding_record_id=result.finding_record_id,
                thread_node_id=link.provider_thread_node_id,
                head_sha=validated_head,
                now=now,
            )

    async def _resolve_one(
        self,
        *,
        run_id: str,
        target: ChangeRequestTarget,
        finding_record_id: str,
        thread_node_id: str,
        head_sha: str,
        now: datetime,
    ) -> None:
        assert self._thread_resolver is not None
        try:
            op = await self._repository.reserve_resolution_operation(
                operation_id=_id("resolution-op", f"{run_id}:{finding_record_id}"),
                finding_record_id=finding_record_id,
                provider_thread_node_id=thread_node_id,
                head_sha=head_sha,
                now=now,
            )
        except PersistenceIntegrityError:
            # Duplicate linkage or a node-id mismatch is an integrity failure: never resolve.
            await self._repository.mark_finding_indeterminate(finding_record_id, now)
            return

        if op.state in {ThreadResolutionState.COMPLETED, ThreadResolutionState.RECONCILED}:
            await self._repository.mark_finding_resolved(
                finding_record_id, head_sha=head_sha, now=now
            )
            return
        if op.state in {
            ThreadResolutionState.KNOWN_REJECTED,
            ThreadResolutionState.INTEGRITY_FAILED,
        }:
            return
        if op.state == ThreadResolutionState.AMBIGUOUS:
            await self._reconcile_resolution(
                op, target, finding_record_id, thread_node_id, head_sha, now
            )
            return
        if op.state == ThreadResolutionState.ATTEMPT_STARTED:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.ATTEMPT_STARTED,
                ThreadResolutionState.AMBIGUOUS,
                now,
                terminal_reason="attempt_recovered_ambiguous",
            )
            await self._reconcile_resolution(
                op, target, finding_record_id, thread_node_id, head_sha, now
            )
            return

        # reserved_unattempted: completely reread the thread before any mutation.
        read = await self._thread_resolver.read_thread(target, thread_node_id=thread_node_id)
        if read.found and read.is_resolved and read.collection_complete:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.RESERVED_UNATTEMPTED,
                ThreadResolutionState.RECONCILED,
                now,
                provider_observed_state="resolved",
            )
            await self._repository.mark_finding_resolved(
                finding_record_id, head_sha=head_sha, now=now
            )
            return
        if not read.collection_complete:
            # The current state could not be confirmed; leave reserved and open for a later run.
            return
        if not read.found:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.RESERVED_UNATTEMPTED,
                ThreadResolutionState.INTEGRITY_FAILED,
                now,
                terminal_reason="linked_thread_absent",
            )
            await self._repository.mark_finding_indeterminate(finding_record_id, now)
            return

        if not await self._repository.transition_resolution_operation(
            op.id,
            ThreadResolutionState.RESERVED_UNATTEMPTED,
            ThreadResolutionState.ATTEMPT_STARTED,
            now,
            increment_attempt=True,
        ):
            return
        try:
            await self._thread_resolver.resolve_thread(target, thread_node_id=thread_node_id)
        except ProviderWriteAmbiguousError:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.ATTEMPT_STARTED,
                ThreadResolutionState.AMBIGUOUS,
                now,
                terminal_reason="resolve_ambiguous",
            )
            await self._reconcile_resolution(
                op, target, finding_record_id, thread_node_id, head_sha, now
            )
            return
        except ProviderWriteRejectedError:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.ATTEMPT_STARTED,
                ThreadResolutionState.KNOWN_REJECTED,
                now,
                terminal_reason="resolve_rejected",
            )
            return
        # Observed success must be verified by a complete reread before completing.
        verify = await self._thread_resolver.read_thread(target, thread_node_id=thread_node_id)
        if verify.found and verify.is_resolved and verify.collection_complete:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.ATTEMPT_STARTED,
                ThreadResolutionState.COMPLETED,
                now,
                provider_observed_state="resolved",
            )
            await self._repository.mark_finding_resolved(
                finding_record_id, head_sha=head_sha, now=now
            )
            return
        await self._repository.transition_resolution_operation(
            op.id,
            ThreadResolutionState.ATTEMPT_STARTED,
            ThreadResolutionState.AMBIGUOUS,
            now,
            terminal_reason="resolve_unverified",
        )
        await self._reconcile_resolution(
            op, target, finding_record_id, thread_node_id, head_sha, now
        )

    async def _reconcile_resolution(
        self,
        op: ThreadResolutionOperation,
        target: ChangeRequestTarget,
        finding_record_id: str,
        thread_node_id: str,
        head_sha: str,
        now: datetime,
    ) -> None:
        assert self._thread_resolver is not None
        read = await self._thread_resolver.read_thread(target, thread_node_id=thread_node_id)
        if read.found and read.is_resolved and read.collection_complete:
            await self._repository.transition_resolution_operation(
                op.id,
                ThreadResolutionState.AMBIGUOUS,
                ThreadResolutionState.RECONCILED,
                now,
                provider_observed_state="resolved",
            )
            await self._repository.mark_finding_resolved(
                finding_record_id, head_sha=head_sha, now=now
            )
            return
        # Without proof of resolution the mutation is never repeated; the finding is left
        # resolution_indeterminate and the operation stays ambiguous for a future reread.
        await self._repository.mark_finding_indeterminate(finding_record_id, now)

    async def deduplicate(
        self,
        *,
        run_id: str,
        current: ChangeRequest,
        artifact: NormalizedReviewArtifact,
        diff: DiffCollection,
        now: datetime,
    ) -> PublicationProjection:
        """Validate identity, upsert finding records/occurrences, and project only-new findings.

        Findings whose structured evidence does not occur in the submitted diff (or whose identity
        is absent/invalid) are omitted. The authoritative artifact is never mutated; exact primary
        duplicates keep their original record and thread and are not republished.
        """
        scope = scope_of(current.target)
        new_findings: list[tuple[Finding, str]] = []
        seen: set[str] = set()
        for ordinal, finding in enumerate(artifact.findings):
            if finding.identity is None:
                continue
            if not finding_identity_is_valid(
                finding.identity, path=finding.path, diff_files=diff.items
            ):
                continue
            primary = primary_fingerprint_v1(
                provider_id=scope.provider_id,
                installation_id=scope.installation_id,
                repository_id=scope.repository_id,
                path=finding.path,
                category=finding.category,
                rule=finding.identity.rule,
                symbol=finding.identity.symbol,
                evidence=finding.identity.evidence,
            )
            record_id = finding_record_id(scope.finding_scope, primary)
            if record_id in seen:
                continue
            seen.add(record_id)
            secondary = secondary_fingerprint_v1(
                stable_repository_identity=scope.stable_identity,
                path=finding.path,
                category=finding.category,
                rule=finding.identity.rule,
                title=finding.title,
            )
            evidence = normalize_multiline(finding.identity.evidence)
            record = FindingRecord(
                id=record_id,
                provider_id=scope.provider_id,
                installation_id=scope.installation_id,
                repository_id=scope.repository_id,
                external_number=scope.external_number,
                fingerprint_version=FINGERPRINT_VERSION,
                primary_fingerprint=primary,
                secondary_fingerprint=secondary,
                path=normalize_path(finding.path),
                category=normalize_category(finding.category),
                rule=normalize_rule(finding.identity.rule),
                symbol=(
                    normalize_symbol(finding.identity.symbol)
                    if finding.identity.symbol is not None
                    else None
                ),
                evidence=evidence,
                evidence_digest=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
                state=FindingLifecycleState.OPEN,
                first_seen_run_id=run_id,
                last_seen_run_id=run_id,
                first_seen_head_sha=current.head_sha,
                last_seen_head_sha=current.head_sha,
                created_at=now,
                updated_at=now,
            )
            durable = await self._repository.upsert_finding(record, now)
            is_new = durable.first_seen_run_id == run_id
            await self._repository.record_occurrence(
                FindingOccurrence(
                    finding_record_id=record_id,
                    review_run_id=run_id,
                    artifact_ordinal=ordinal,
                    observed_head_sha=current.head_sha,
                    path=finding.path,
                    line=finding.line,
                    disposition=(
                        OccurrenceDisposition.NEW
                        if is_new
                        else OccurrenceDisposition.EXACT_DUPLICATE
                    ),
                    publication_eligible=is_new,
                    created_at=now,
                )
            )
            if is_new:
                new_findings.append((finding, record_id))
        return PublicationProjection(new=tuple(new_findings))

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

    async def run_revalidation(
        self,
        *,
        run_id: str,
        current: ChangeRequest,
        diff: DiffCollection,
        policy: EffectiveReviewPolicy,
        profile: ResolvedModelProfile,
        now: datetime,
    ) -> bool:
        """Select, collect, and revalidate priors. Returns True when the run was superseded."""
        existing = await self._repository.get_revalidation_run(run_id)
        if existing is not None and existing.state == RevalidationRunState.COMPLETED:
            return False
        if existing is not None and existing.state == RevalidationRunState.SUPERSEDED:
            return True
        selection = await self.select_priors(
            run_id=run_id, current=current, diff=diff, policy=policy, now=now
        )
        if not selection.selected:
            await self._complete_run(run_id, now)
            return False
        content = await self.collect_content(
            run_id=run_id,
            target=current.target,
            validated_head=current.head_sha,
            selected=selection.selected,
            policy=policy,
            now=now,
        )
        if content.superseded:
            await self._repository.transition_revalidation_run(
                run_id, RevalidationRunState.SELECTED, RevalidationRunState.SUPERSEDED, now
            )
            return True
        await self._revalidate_batch(
            run_id=run_id,
            head_sha=current.head_sha,
            items=content.items,
            profile=profile,
            now=now,
        )
        await self._complete_run(run_id, now)
        return False

    async def _complete_run(self, run_id: str, now: datetime) -> None:
        run = await self._repository.get_revalidation_run(run_id)
        if run is not None and run.state == RevalidationRunState.SELECTED:
            await self._repository.transition_revalidation_run(
                run_id, RevalidationRunState.SELECTED, RevalidationRunState.COMPLETED, now
            )

    async def _revalidate_batch(
        self,
        *,
        run_id: str,
        head_sha: str,
        items: list[tuple[FindingRecord, str]],
        profile: ResolvedModelProfile,
        now: datetime,
    ) -> None:
        if not items:
            return
        if self._revalidator is None:
            for record, _ in items:
                await self._persist_uncertain(
                    run_id, record, REASON_REVALIDATION_UNAVAILABLE, head_sha, now
                )
            return
        record_by_id = {record.id: record for record, _ in items}
        request_items = [
            RevalidationRequestItem(
                finding_id=record.id,
                path=record.path,
                rule=record.rule,
                symbol=record.symbol,
                evidence=record.evidence,
                current_file_content=content,
            )
            for record, content in items
        ]
        existing = await self._repository.latest_revalidation_call(run_id)
        payload: dict[str, object] | None = None
        while request_items:
            request = FindingRevalidationRequest(
                head_sha=head_sha, items=tuple(request_items), model_profile=profile
            )
            try:
                payload = await self._revalidator.preflight_revalidation(request)
                break
            except IncompleteReviewInputError:
                dropped = request_items.pop()
                await self._persist_uncertain(
                    run_id,
                    record_by_id[dropped.finding_id],
                    REASON_REVALIDATION_LIMIT,
                    head_sha,
                    now,
                )
        if not request_items or payload is None:
            return
        batch_ids = [item.finding_id for item in request_items]

        ordinal = 1 if existing is None else existing.identity.batch_ordinal
        if existing is not None and existing.state == RevalidationCallState.RETRYABLE_REJECTED:
            ordinal += 1
            existing = None
        identity = RevalidationCallIdentity(
            id=_id("revalidation-call", f"{run_id}:{ordinal}"),
            review_run_id=run_id,
            batch_ordinal=ordinal,
            provider_id=str(profile.provider_id),
            model_profile_id=profile.alias.value,
            model_profile_version=profile.profile_version,
            prompt_version=REVALIDATION_PROMPT_VERSION,
            schema_version=REVALIDATION_SCHEMA_VERSION,
            estimated_input_tokens=_estimated_tokens(payload),
        )
        call = existing or await self._repository.reserve_revalidation_call(identity, now)

        if call.state == RevalidationCallState.ATTEMPT_STARTED:
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.ATTEMPT_STARTED,
                RevalidationCallState.AMBIGUOUS,
                now,
                usage=UsageDisposition(status="unknown"),
            )
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_AMBIGUOUS, head_sha, now
            )
            return
        if call.state == RevalidationCallState.RESPONSE_OBSERVED:
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_RESPONSE_RECOVERY, head_sha, now
            )
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.RESPONSE_OBSERVED,
                RevalidationCallState.COMPLETED,
                now,
            )
            return
        if call.state == RevalidationCallState.AMBIGUOUS:
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_AMBIGUOUS, head_sha, now
            )
            return
        if call.state in {
            RevalidationCallState.TERMINAL_REJECTED,
            RevalidationCallState.KNOWN_REJECTED,
        }:
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_REJECTED, head_sha, now
            )
            return
        if call.state == RevalidationCallState.COMPLETED:
            return

        if not await self._repository.transition_revalidation_call(
            call.identity.id,
            RevalidationCallState.RESERVED,
            RevalidationCallState.ATTEMPT_STARTED,
            now,
        ):
            raise RuntimeError("revalidation attempt could not start")
        try:
            batch = await self._revalidator.generate_revalidation(payload)
        except ProviderCallSafeRetryError:
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.ATTEMPT_STARTED,
                RevalidationCallState.RETRYABLE_REJECTED,
                now,
            )
            raise
        except ProviderCallAmbiguousError:
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.ATTEMPT_STARTED,
                RevalidationCallState.AMBIGUOUS,
                now,
                usage=UsageDisposition(status="unknown"),
            )
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_AMBIGUOUS, head_sha, now
            )
            return
        except ProviderCallObservedTerminalError as error:
            usage = error.usage if isinstance(error.usage, TokenUsage) else None
            if usage is None:
                raise RuntimeError("observed terminal revalidation lacked usage") from None
            await self._observe_then_complete(
                call.identity.id,
                run_id,
                batch_ids,
                REASON_REVALIDATION_MALFORMED,
                head_sha,
                usage,
                now,
            )
            return
        except ProviderCallObservedInvalidResponseError:
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.ATTEMPT_STARTED,
                RevalidationCallState.RESPONSE_OBSERVED,
                now,
                usage=UsageDisposition(status="unknown"),
            )
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_MALFORMED, head_sha, now
            )
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.RESPONSE_OBSERVED,
                RevalidationCallState.COMPLETED,
                now,
            )
            return
        except MalformedProviderOutputError as error:
            usage = error.usage if isinstance(error.usage, TokenUsage) else None
            if usage is None:
                raise RuntimeError("malformed revalidation output lacked usage") from None
            await self._observe_then_complete(
                call.identity.id,
                run_id,
                batch_ids,
                REASON_REVALIDATION_MALFORMED,
                head_sha,
                usage,
                now,
            )
            return
        except (ProviderCallRejectedError, ProviderCallTerminalError):
            await self._repository.transition_revalidation_call(
                call.identity.id,
                RevalidationCallState.ATTEMPT_STARTED,
                RevalidationCallState.TERMINAL_REJECTED,
                now,
            )
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_REJECTED, head_sha, now
            )
            return

        await self._repository.transition_revalidation_call(
            call.identity.id,
            RevalidationCallState.ATTEMPT_STARTED,
            RevalidationCallState.RESPONSE_OBSERVED,
            now,
            usage=UsageDisposition(status="known", usage=batch.usage),
        )
        by_id: dict[str, RevalidationOutcome] = {}
        current_paths: dict[str, str | None] = {}
        valid = True
        for item in batch.results:
            if item.finding_id not in record_by_id or item.finding_id in by_id:
                valid = False
                break
            by_id[item.finding_id] = item.outcome
            current_paths[item.finding_id] = item.current_path
        if valid and set(by_id) != set(batch_ids):
            valid = False
        if not valid:
            await self._mark_batch_uncertain(
                run_id, batch_ids, REASON_REVALIDATION_MALFORMED, head_sha, now
            )
        else:
            for finding_id in batch_ids:
                outcome = by_id[finding_id]
                await self._repository.persist_revalidation_result(
                    FindingRevalidationResult(
                        review_run_id=run_id,
                        finding_record_id=finding_id,
                        outcome=outcome,
                        reason_code=f"model_{outcome}",
                        fetched_ref=head_sha,
                        current_path=current_paths[finding_id],
                        created_at=now,
                        updated_at=now,
                    )
                )
        await self._repository.transition_revalidation_call(
            call.identity.id,
            RevalidationCallState.RESPONSE_OBSERVED,
            RevalidationCallState.COMPLETED,
            now,
        )

    async def _observe_then_complete(
        self,
        call_id: str,
        run_id: str,
        batch_ids: list[str],
        reason: str,
        head_sha: str,
        usage: TokenUsage,
        now: datetime,
    ) -> None:
        await self._repository.transition_revalidation_call(
            call_id,
            RevalidationCallState.ATTEMPT_STARTED,
            RevalidationCallState.RESPONSE_OBSERVED,
            now,
            usage=UsageDisposition(status="known", usage=usage),
        )
        await self._mark_batch_uncertain(run_id, batch_ids, reason, head_sha, now)
        await self._repository.transition_revalidation_call(
            call_id,
            RevalidationCallState.RESPONSE_OBSERVED,
            RevalidationCallState.COMPLETED,
            now,
        )

    async def _mark_batch_uncertain(
        self, run_id: str, finding_ids: list[str], reason: str, fetched_ref: str, now: datetime
    ) -> None:
        for finding_id in finding_ids:
            await self._repository.persist_revalidation_result(
                FindingRevalidationResult(
                    review_run_id=run_id,
                    finding_record_id=finding_id,
                    outcome=RevalidationOutcome.UNCERTAIN,
                    reason_code=reason,
                    fetched_ref=fetched_ref,
                    current_path=None,
                    created_at=now,
                    updated_at=now,
                )
            )

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
