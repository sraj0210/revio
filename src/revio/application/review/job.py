"""Phase 4 durable review job execution and GitHub publication."""

import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from revio.application.review.execution import ReviewGenerationService, build_artifact
from revio.application.review.markers import marker_key_id, review_marker
from revio.config.review import ReviewSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.models import ChangeRequest, DiffCollection
from revio.domain.queue import JobLease
from revio.domain.reviews import (
    NormalizedReviewArtifact,
    PartialReason,
    ReviewRunState,
    ReviewStatusDetails,
    WriteOperationRecord,
    WriteOperationState,
)
from revio.errors import (
    ProviderWriteAmbiguousError,
    ProviderWriteRejectedError,
    ReconciliationIntegrityError,
)
from revio.ports.persistence import ReviewRepository
from revio.ports.scm import ChangeRequestReadPort, ReviewWriterPort


def stable_id(kind: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"revio:{kind}:{value}"))


class ReviewJobExecutor:
    def __init__(
        self,
        repository: ReviewRepository,
        reader: ChangeRequestReadPort,
        generation: ReviewGenerationService,
        profile: ResolvedModelProfile,
        settings: ReviewSettings,
        *,
        writer: ReviewWriterPort | None = None,
        marker_key: bytes | None = None,
    ) -> None:
        self._repository = repository
        self._reader = reader
        self._generation = generation
        self._profile = profile
        self._settings = settings
        self._writer = writer
        self._marker_key = marker_key
        if settings.review_publish_enabled and (writer is None or marker_key is None):
            raise ValueError("publishing requires GitHub writer and marker key")

    async def _create_or_reconcile_check(
        self,
        lease: JobLease,
        artifact: NormalizedReviewArtifact,
        now: datetime,
    ) -> WriteOperationRecord:
        assert self._writer is not None
        target = lease.job.event.change_request
        assert target is not None
        external_id = stable_id("check-run", artifact.review_run_id)
        operation_id = stable_id("check-operation", artifact.review_run_id)
        operation = await self._repository.reserve_write_operation(
            "check_run",
            operation_id=operation_id,
            run_id=artifact.review_run_id,
            head_sha=lease.job.event.event_head_sha or "",
            external_id=external_id,
            now=now,
        )
        if operation.provider_id is not None:
            return operation
        reconciliation = await self._writer.reconcile_check_run(
            target, head_sha=operation.head_sha, external_id=external_id
        )
        if reconciliation.match_count > 1:
            await self._repository.transition_write_operation(
                "check_run",
                operation.id,
                operation.state,
                WriteOperationState.INTEGRITY_FAILED,
                now,
                terminal_reason="multiple_exact_matches",
            )
            raise ReconciliationIntegrityError("multiple exact Check Runs found")
        if reconciliation.match_count == 1:
            await self._repository.transition_write_operation(
                "check_run",
                operation.id,
                operation.state,
                WriteOperationState.RECONCILED,
                now,
                provider_id=reconciliation.provider_ids[0],
            )
            return WriteOperationRecord(
                **operation.model_dump(exclude={"state", "provider_id"}),
                state=WriteOperationState.RECONCILED,
                provider_id=reconciliation.provider_ids[0],
            )
        if operation.state != WriteOperationState.RESERVED_UNATTEMPTED:
            return operation
        if not reconciliation.actionable_zero:
            return operation
        if not await self._repository.transition_write_operation(
            "check_run",
            operation.id,
            WriteOperationState.RESERVED_UNATTEMPTED,
            WriteOperationState.ATTEMPT_STARTED,
            now,
            increment_attempt=True,
        ):
            raise RuntimeError("Check Run attempt could not start")
        details = ReviewStatusDetails(
            external_id=external_id,
            head_sha=operation.head_sha,
            status="queued",
            summary="Review queued.",
        )
        try:
            provider_id = await self._writer.create_check_run(target, details)
        except ProviderWriteAmbiguousError:
            await self._repository.transition_write_operation(
                "check_run",
                operation.id,
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.AMBIGUOUS,
                now,
                terminal_reason="create_ambiguous",
            )
            after = await self._writer.reconcile_check_run(
                target, head_sha=operation.head_sha, external_id=external_id
            )
            if after.match_count == 1:
                await self._repository.transition_write_operation(
                    "check_run",
                    operation.id,
                    WriteOperationState.AMBIGUOUS,
                    WriteOperationState.RECONCILED,
                    now,
                    provider_id=after.provider_ids[0],
                )
                provider_id = after.provider_ids[0]
            else:
                await self._repository.transition_run(
                    artifact.review_run_id,
                    ReviewRunState.ARTIFACT_DURABLE,
                    ReviewRunState.CHECK_RUN_INDETERMINATE,
                    now,
                    reason="check_run_create_indeterminate",
                )
                return WriteOperationRecord(
                    **operation.model_dump(exclude={"state"}),
                    state=WriteOperationState.AMBIGUOUS,
                )
        else:
            await self._repository.transition_write_operation(
                "check_run",
                operation.id,
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.COMPLETED,
                now,
                provider_id=provider_id,
            )
        return WriteOperationRecord(
            **operation.model_dump(exclude={"state", "provider_id"}),
            state=WriteOperationState.COMPLETED,
            provider_id=provider_id,
        )

    async def _update_check(
        self,
        lease: JobLease,
        operation: WriteOperationRecord,
        artifact: NormalizedReviewArtifact,
        summary: str,
        *,
        neutral: bool,
    ) -> None:
        if self._writer is None or operation.provider_id is None:
            return
        target = lease.job.event.change_request
        assert target is not None
        details = ReviewStatusDetails(
            external_id=operation.external_id or stable_id("check-run", artifact.review_run_id),
            head_sha=operation.head_sha,
            status="completed",
            conclusion="neutral" if neutral else "success",
            summary=summary,
        )
        try:
            await self._writer.update_check_run(target, operation.provider_id, details)
        except ProviderWriteAmbiguousError:
            remote = await self._writer.get_check_run(target, operation.provider_id)
            output = remote.get("output")
            output_mapping = cast(dict[object, object], output) if isinstance(output, dict) else {}
            applied = (
                remote.get("status") == "completed"
                and remote.get("conclusion") == details.conclusion
                and output_mapping.get("summary") == summary
            )
            if not applied:
                raise

    async def _publish(
        self,
        lease: JobLease,
        artifact: NormalizedReviewArtifact,
        diff: DiffCollection,
        check: WriteOperationRecord,
        now: datetime,
    ) -> Literal["completed", "partial", "superseded", "publication_indeterminate"]:
        assert self._writer is not None and self._marker_key is not None
        target = lease.job.event.change_request
        assert target is not None
        current = await self._reader.get_change_request(target)
        if current.head_sha != check.head_sha:
            await self._update_check(
                lease, check, artifact, "Superseded by newer head.", neutral=True
            )
            await self._repository.transition_run(
                artifact.review_run_id,
                ReviewRunState.ARTIFACT_DURABLE,
                ReviewRunState.SUPERSEDED,
                now,
                reason="newer_head",
            )
            return "superseded"
        operation_key = stable_id("publish-key", artifact.review_run_id)
        marker = review_marker(self._marker_key, operation_key)
        operation = await self._repository.reserve_write_operation(
            "publish",
            operation_id=stable_id("publish-operation", artifact.review_run_id),
            run_id=artifact.review_run_id,
            head_sha=current.head_sha,
            operation_key=operation_key,
            marker=marker,
            marker_key_id=marker_key_id(self._marker_key),
            now=now,
        )
        reconciliation = await self._writer.reconcile_review(target, marker=marker)
        if reconciliation.match_count > 1:
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                operation.state,
                WriteOperationState.INTEGRITY_FAILED,
                now,
                terminal_reason="multiple_exact_matches",
            )
            raise ReconciliationIntegrityError("multiple exact reviews found")
        if reconciliation.match_count == 1:
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                operation.state,
                WriteOperationState.RECONCILED,
                now,
                provider_id=reconciliation.provider_ids[0],
            )
            await self._update_check(
                lease, check, artifact, artifact.summary, neutral=artifact.partial
            )
            return "partial" if artifact.partial else "completed"
        if operation.state != WriteOperationState.RESERVED_UNATTEMPTED:
            return "publication_indeterminate"
        if not reconciliation.actionable_zero:
            return "publication_indeterminate"
        findings = (
            ()
            if artifact.partial
            else self._writer.validate_inline_findings(artifact.findings, diff)
        )
        if len(findings) != len(artifact.findings):
            previous_digest = artifact.digest
            replacement = build_artifact(
                run_id=artifact.review_run_id,
                profile=self._profile,
                summary=artifact.summary,
                findings=(),
                partial=True,
                reasons=(PartialReason.ANCHOR_INVALID,),
                now=now,
            )
            if not await self._repository.replace_artifact(
                replacement, expected_digest=previous_digest
            ):
                raise RuntimeError("review artifact anchor downgrade lost its CAS race")
            artifact = replacement
            findings = ()
        await self._repository.transition_write_operation(
            "publish",
            operation.id,
            WriteOperationState.RESERVED_UNATTEMPTED,
            WriteOperationState.ATTEMPT_STARTED,
            now,
            increment_attempt=True,
        )
        try:
            provider_id = await self._writer.publish_review(
                target,
                summary=artifact.summary,
                findings=findings,
                marker=marker,
                commit_id=current.head_sha,
            )
        except ProviderWriteRejectedError:
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.KNOWN_REJECTED,
                now,
                terminal_reason="anchor_validation",
            )
            current_again = await self._reader.get_change_request(target)
            complete = await self._writer.reconcile_review(target, marker=marker)
            if current_again.head_sha != current.head_sha or not complete.actionable_zero:
                return "partial"
            partial_artifact = build_artifact(
                run_id=artifact.review_run_id,
                profile=self._profile,
                summary=artifact.summary,
                findings=(),
                partial=True,
                reasons=(PartialReason.ANCHOR_RACE,),
                now=now,
            )
            await self._repository.replace_artifact(
                partial_artifact, expected_digest=artifact.digest
            )
            artifact = partial_artifact
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                WriteOperationState.KNOWN_REJECTED,
                WriteOperationState.ATTEMPT_STARTED,
                now,
                increment_attempt=True,
            )
            try:
                provider_id = await self._writer.publish_review(
                    target,
                    summary=artifact.summary,
                    findings=(),
                    marker=marker,
                    commit_id=current.head_sha,
                )
            except ProviderWriteAmbiguousError:
                await self._repository.transition_write_operation(
                    "publish",
                    operation.id,
                    WriteOperationState.ATTEMPT_STARTED,
                    WriteOperationState.AMBIGUOUS,
                    now,
                    terminal_reason="fallback_ambiguous",
                )
                provider_id = ""
        except ProviderWriteAmbiguousError:
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.AMBIGUOUS,
                now,
                terminal_reason="publish_ambiguous",
            )
            provider_id = ""
        if not provider_id:
            after = await self._writer.reconcile_review(target, marker=marker)
            if after.match_count > 1:
                await self._repository.transition_write_operation(
                    "publish",
                    operation.id,
                    WriteOperationState.AMBIGUOUS,
                    WriteOperationState.INTEGRITY_FAILED,
                    now,
                    terminal_reason="multiple_exact_matches",
                )
                raise ReconciliationIntegrityError("multiple exact reviews found")
            if after.match_count == 1:
                provider_id = after.provider_ids[0]
                await self._repository.transition_write_operation(
                    "publish",
                    operation.id,
                    WriteOperationState.AMBIGUOUS,
                    WriteOperationState.RECONCILED,
                    now,
                    provider_id=provider_id,
                )
            else:
                await self._repository.transition_run(
                    artifact.review_run_id,
                    ReviewRunState.ARTIFACT_DURABLE,
                    ReviewRunState.PUBLICATION_INDETERMINATE,
                    now,
                    reason="publication_indeterminate",
                )
                await self._update_check(
                    lease,
                    check,
                    artifact,
                    "Review publication outcome could not be confirmed.",
                    neutral=True,
                )
                return "publication_indeterminate"
        else:
            await self._repository.transition_write_operation(
                "publish",
                operation.id,
                WriteOperationState.ATTEMPT_STARTED,
                WriteOperationState.COMPLETED,
                now,
                provider_id=provider_id,
            )
        await self._update_check(lease, check, artifact, artifact.summary, neutral=artifact.partial)
        return "partial" if artifact.partial else "completed"

    async def execute(
        self,
        lease: JobLease,
        current: ChangeRequest,
        *,
        now: datetime | None = None,
    ) -> Literal[
        "completed", "partial", "superseded", "publication_indeterminate", "check_run_indeterminate"
    ]:
        current_time = now or datetime.now(UTC)
        run_id = stable_id("review-run", lease.job.id)
        run = await self._repository.create_run(
            run_id=run_id,
            job_id=lease.job.id,
            head_sha=current.head_sha,
            base_sha=current.base_sha,
            external_id=stable_id("check-run", run_id),
            now=current_time,
        )
        diff = await self._reader.get_diff(current.target)
        artifact = await self._generation.generate(
            run_id=run.id,
            change_request=current,
            diff=diff,
            profile=self._profile,
            now=current_time,
        )
        if not self._settings.review_publish_enabled:
            await self._repository.transition_run(
                run.id,
                ReviewRunState.ARTIFACT_DURABLE,
                ReviewRunState.PARTIAL if artifact.partial else ReviewRunState.COMPLETED,
                current_time,
            )
            return "partial" if artifact.partial else "completed"
        check = await self._create_or_reconcile_check(lease, artifact, current_time)
        if check.provider_id is None:
            return "check_run_indeterminate"
        return await self._publish(lease, artifact, diff, check, current_time)
