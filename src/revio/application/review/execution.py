"""Restart-safe Phase 4 generation, normalization, and artifact persistence."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Protocol

from revio.config.review import ReviewSettings
from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.models import (
    ChangeRequest,
    DiffCollection,
    DiffFile,
    Finding,
    ReviewRequest,
    ReviewResult,
    TokenUsage,
)
from revio.domain.reviews import (
    DiffEnvelope,
    NormalizedReviewArtifact,
    PartialReason,
    ProviderCallIdentity,
    ProviderCallState,
    ReviewRunState,
    UsageDisposition,
)
from revio.errors import (
    IncompleteReviewInputError,
    MalformedProviderOutputError,
    ProviderCallAmbiguousError,
    ProviderCallRejectedError,
    ProviderCallSafeRetryError,
    ProviderCallTerminalError,
)
from revio.ports.persistence import ReviewRepository

PROMPT_VERSION = "review-v1"
SCHEMA_VERSION = "review-v1"
ARTIFACT_VERSION = "review-artifact-v1"


class PreflightedReviewer(Protocol):
    async def preflight(self, request: ReviewRequest) -> dict[str, object]: ...
    async def generate_preflighted(self, payload: dict[str, object]) -> ReviewResult: ...
    async def repair_preflight(self, payload: dict[str, object]) -> dict[str, object]: ...


def _id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"revio:{namespace}:{value}"))


class DiffLimiter:
    def __init__(self, settings: ReviewSettings) -> None:
        self._settings = settings

    def limit(self, collection: DiffCollection) -> tuple[tuple[DiffFile, ...], DiffEnvelope]:
        accepted: list[DiffFile] = []
        total_lines = 0
        total_bytes = 0
        reasons: list[str] = []
        patch_complete = True
        for item in collection.items:
            if len(accepted) >= self._settings.review_max_files:
                reasons.append("service_file_limit")
                break
            if item.patch_state != "complete":
                patch_complete = False
                reasons.append(f"patch_{item.patch_state}")
                continue
            retained = item
            encoded = json.dumps(retained.model_dump(mode="json"), separators=(",", ":")).encode()
            line_budget = self._settings.review_max_lines - total_lines
            byte_budget = min(
                self._settings.review_max_patch_bytes,
                self._settings.review_max_bytes - total_bytes,
            )
            if len(item.lines) > line_budget:
                retained = item.model_copy(update={"lines": item.lines[: max(line_budget, 0)]})
                reasons.append("service_line_limit")
            while retained.lines:
                encoded = json.dumps(
                    retained.model_dump(mode="json"), separators=(",", ":")
                ).encode()
                if len(encoded) <= byte_budget:
                    break
                retained = retained.model_copy(update={"lines": retained.lines[:-1]})
                reasons.append(
                    "service_patch_byte_limit"
                    if byte_budget == self._settings.review_max_patch_bytes
                    else "service_byte_limit"
                )
            encoded = json.dumps(retained.model_dump(mode="json"), separators=(",", ":")).encode()
            if not retained.lines or len(encoded) > byte_budget:
                if byte_budget <= 0:
                    reasons.append("service_byte_limit")
                break
            accepted.append(retained)
            total_lines += len(retained.lines)
            total_bytes += len(encoded)
            if len(retained.lines) != len(item.lines):
                break
        expected = collection.expected_file_count
        returned = len({(item.old_path, item.new_path, item.status) for item in collection.items})
        if expected is None:
            expected = returned
            reasons.append("expected_file_count_unavailable")
        envelope = DiffEnvelope(
            expected_file_count=expected,
            returned_unique_file_count=returned,
            provider_collection_complete=collection.completeness.is_complete,
            patch_complete=patch_complete,
            service_truncation_reasons=tuple(dict.fromkeys(reasons)),
            file_count=len(accepted),
            line_count=total_lines,
            byte_count=total_bytes,
            estimated_tokens=0,
        )
        return tuple(accepted), envelope


def _artifact_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_artifact(
    *,
    run_id: str,
    profile: ResolvedModelProfile,
    summary: str,
    findings: tuple[Finding, ...],
    partial: bool,
    reasons: tuple[PartialReason, ...],
    now: datetime,
) -> NormalizedReviewArtifact:
    if "```" in summary or any("```" in item.explanation for item in findings):
        summary = "Review output was omitted because it did not satisfy content limits."
        findings = ()
        partial = True
        reasons = tuple(dict.fromkeys((*reasons, PartialReason.OUTPUT_INVALID)))
    digest_input: dict[str, object] = {
        "run_id": run_id,
        "artifact_version": ARTIFACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "profile": profile.alias.value,
        "profile_version": profile.profile_version,
        "summary": summary,
        "findings": [item.model_dump(mode="json") for item in findings],
        "partial": partial,
        "reasons": [str(item) for item in reasons],
    }
    return NormalizedReviewArtifact(
        review_run_id=run_id,
        artifact_version=ARTIFACT_VERSION,
        schema_version=SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        model_profile_id=profile.alias.value,
        model_profile_version=profile.profile_version,
        summary=summary,
        findings=findings,
        partial=partial,
        reason_codes=reasons,
        digest=_artifact_digest(digest_input),
        created_at=now,
        updated_at=now,
    )


class ReviewGenerationService:
    def __init__(
        self,
        repository: ReviewRepository,
        reviewer: PreflightedReviewer,
        settings: ReviewSettings,
    ) -> None:
        self._repository = repository
        self._reviewer = reviewer
        self._settings = settings
        self._limiter = DiffLimiter(settings)

    async def _fallback(
        self,
        run_id: str,
        profile: ResolvedModelProfile,
        reason: PartialReason,
        summary: str,
        now: datetime,
        expected_state: ReviewRunState = ReviewRunState.GENERATION_ATTEMPTED,
    ) -> NormalizedReviewArtifact:
        artifact = build_artifact(
            run_id=run_id,
            profile=profile,
            summary=summary,
            findings=(),
            partial=True,
            reasons=(reason,),
            now=now,
        )
        if not await self._repository.persist_artifact(artifact, expected_state):
            existing = await self._repository.get_artifact(run_id)
            if existing is None:
                raise RuntimeError("review artifact could not be persisted")
            return existing
        return artifact

    async def generate(
        self,
        *,
        run_id: str,
        change_request: ChangeRequest,
        diff: DiffCollection,
        profile: ResolvedModelProfile,
        now: datetime | None = None,
    ) -> NormalizedReviewArtifact:
        current_time = now or datetime.now(UTC)
        existing = await self._repository.get_artifact(run_id)
        if existing is not None:
            return existing
        files, envelope = self._limiter.limit(diff)
        if envelope.partial:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.INPUT_INCOMPLETE,
                "Review input was incomplete; no inline findings were published.",
                current_time,
                ReviewRunState.GENERATION_PENDING,
            )
        request = ReviewRequest(
            change_request=change_request,
            diff_files=files,
            model_profile=profile,
        )
        token_reduced = False
        while True:
            try:
                payload = await self._reviewer.preflight(request)
                break
            except IncompleteReviewInputError:
                if len(request.diff_files) <= 1:
                    return await self._fallback(
                        run_id,
                        profile,
                        PartialReason.INPUT_TRUNCATED,
                        "Review input exceeded the approved token admission ceiling.",
                        current_time,
                        ReviewRunState.GENERATION_PENDING,
                    )
                token_reduced = True
                request = request.model_copy(update={"diff_files": request.diff_files[:-1]})
            except ProviderCallTerminalError:
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.PROVIDER_REFUSAL,
                    "AI review preflight was rejected.",
                    current_time,
                    ReviewRunState.GENERATION_PENDING,
                )
        latest = await self._repository.latest_call(run_id, "initial")
        ordinal = 1 if latest is None else latest.identity.call_ordinal
        if latest is not None and latest.state == ProviderCallState.KNOWN_REJECTED:
            ordinal += 1
            latest = None
        call_identity = ProviderCallIdentity(
            id=_id("provider-call", f"{run_id}:initial:{ordinal}"),
            review_run_id=run_id,
            call_kind="initial",
            call_ordinal=ordinal,
            provider_id=str(profile.provider_id),
            model_profile_id=profile.alias.value,
            model_profile_version=profile.profile_version,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        call = latest or await self._repository.reserve_call(call_identity, current_time)
        if call.state == ProviderCallState.ATTEMPT_STARTED:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.AMBIGUOUS,
                current_time,
                usage=UsageDisposition(status="unknown"),
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.PROVIDER_CALL_AMBIGUOUS,
                "AI review outcome could not be confirmed.",
                current_time,
            )
        if call.state == ProviderCallState.RESPONSE_OBSERVED:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.RESPONSE_RECOVERY_UNAVAILABLE,
                "AI review response could not be recovered after restart.",
                current_time,
            )
        if call.state == ProviderCallState.AMBIGUOUS:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.PROVIDER_CALL_AMBIGUOUS,
                "AI review outcome was unavailable.",
                current_time,
            )
        if call.state == ProviderCallState.COMPLETED:
            artifact = await self._repository.get_artifact(run_id)
            if artifact is not None:
                return artifact
            return await self._repair(
                run_id=run_id,
                profile=profile,
                payload=payload,
                now=current_time,
            )
        if not await self._repository.transition_call(
            call.identity.id,
            ProviderCallState.RESERVED,
            ProviderCallState.ATTEMPT_STARTED,
            current_time,
        ):
            raise RuntimeError("provider call attempt could not start")
        await self._repository.transition_run(
            run_id,
            ReviewRunState.GENERATION_PENDING,
            ReviewRunState.GENERATION_ATTEMPTED,
            current_time,
        )
        try:
            result = await self._reviewer.generate_preflighted(payload)
        except MalformedProviderOutputError as error:
            if not isinstance(error.usage, TokenUsage):
                raise RuntimeError("malformed output lacked normalized usage") from None
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.RESPONSE_OBSERVED,
                current_time,
                usage=UsageDisposition(status="known", usage=error.usage),
            )
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.RESPONSE_OBSERVED,
                ProviderCallState.COMPLETED,
                current_time,
            )
            return await self._repair(
                run_id=run_id, profile=profile, payload=payload, now=current_time
            )
        except ProviderCallAmbiguousError:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.AMBIGUOUS,
                current_time,
                usage=UsageDisposition(status="unknown"),
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.PROVIDER_CALL_AMBIGUOUS,
                "AI review outcome could not be confirmed.",
                current_time,
            )
        except ProviderCallSafeRetryError:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.KNOWN_REJECTED,
                current_time,
            )
            raise
        except (ProviderCallRejectedError, ProviderCallTerminalError):
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.KNOWN_REJECTED,
                current_time,
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.PROVIDER_REFUSAL,
                "AI review generation was rejected.",
                current_time,
            )
        await self._repository.transition_call(
            call.identity.id,
            ProviderCallState.ATTEMPT_STARTED,
            ProviderCallState.RESPONSE_OBSERVED,
            current_time,
            usage=UsageDisposition(status="known", usage=result.usage),
        )
        accepted = tuple(
            item
            for item in result.findings
            if item.confidence >= self._settings.review_summary_confidence
        )
        overflow = len(accepted) > self._settings.review_max_findings
        partial = overflow or token_reduced
        routed = tuple(
            item.model_copy(
                update={
                    "inline_eligible": item.confidence >= self._settings.review_inline_confidence
                }
            )
            for item in accepted[: self._settings.review_max_findings]
        )
        artifact = build_artifact(
            run_id=run_id,
            profile=profile,
            summary=result.summary,
            findings=() if partial else routed,
            partial=partial,
            reasons=(
                (PartialReason.FINDINGS_TRUNCATED,)
                if overflow
                else (PartialReason.INPUT_TRUNCATED,)
                if token_reduced
                else ()
            ),
            now=current_time,
        )
        await self._repository.persist_artifact(artifact, ReviewRunState.GENERATION_ATTEMPTED)
        await self._repository.transition_call(
            call.identity.id,
            ProviderCallState.RESPONSE_OBSERVED,
            ProviderCallState.COMPLETED,
            current_time,
        )
        return artifact

    async def _repair(
        self,
        *,
        run_id: str,
        profile: ResolvedModelProfile,
        payload: dict[str, object],
        now: datetime,
    ) -> NormalizedReviewArtifact:
        try:
            repair_payload = await self._reviewer.repair_preflight(payload)
        except (IncompleteReviewInputError, ProviderCallTerminalError):
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review output could not be validated.",
                now,
            )
        latest = await self._repository.latest_call(run_id, "repair")
        ordinal = 1 if latest is None else latest.identity.call_ordinal
        if latest is not None and latest.state == ProviderCallState.KNOWN_REJECTED:
            ordinal += 1
            latest = None
        identity = ProviderCallIdentity(
            id=_id("provider-call", f"{run_id}:repair:{ordinal}"),
            review_run_id=run_id,
            call_kind="repair",
            call_ordinal=ordinal,
            provider_id=str(profile.provider_id),
            model_profile_id=profile.alias.value,
            model_profile_version=profile.profile_version,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        call = latest or await self._repository.reserve_call(identity, now)
        if call.state == ProviderCallState.ATTEMPT_STARTED:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.AMBIGUOUS,
                now,
                usage=UsageDisposition(status="unknown"),
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review repair outcome was unavailable.",
                now,
            )
        if call.state in {ProviderCallState.AMBIGUOUS, ProviderCallState.RESPONSE_OBSERVED}:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review repair outcome was unavailable.",
                now,
            )
        if call.state == ProviderCallState.COMPLETED:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review repair response could not be recovered.",
                now,
            )
        if not await self._repository.transition_call(
            call.identity.id, ProviderCallState.RESERVED, ProviderCallState.ATTEMPT_STARTED, now
        ):
            raise RuntimeError("repair attempt could not start")
        try:
            result = await self._reviewer.generate_preflighted(repair_payload)
        except ProviderCallSafeRetryError:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.KNOWN_REJECTED,
                now,
            )
            raise
        except ProviderCallAmbiguousError:
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.AMBIGUOUS,
                now,
                usage=UsageDisposition(status="unknown"),
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review repair outcome could not be confirmed.",
                now,
            )
        except MalformedProviderOutputError as error:
            if not isinstance(error.usage, TokenUsage):
                raise RuntimeError("repair output lacked normalized usage") from None
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.RESPONSE_OBSERVED,
                now,
                usage=UsageDisposition(status="known", usage=error.usage),
            )
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.RESPONSE_OBSERVED,
                ProviderCallState.COMPLETED,
                now,
            )
            return await self._fallback(
                run_id,
                profile,
                PartialReason.REPAIR_FAILED,
                "AI review output could not be validated after one repair.",
                now,
            )
        except (ProviderCallRejectedError, ProviderCallTerminalError):
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.KNOWN_REJECTED,
                now,
            )
            return await self._fallback(
                run_id, profile, PartialReason.REPAIR_FAILED, "AI review repair was rejected.", now
            )
        await self._repository.transition_call(
            call.identity.id,
            ProviderCallState.ATTEMPT_STARTED,
            ProviderCallState.RESPONSE_OBSERVED,
            now,
            usage=UsageDisposition(status="known", usage=result.usage),
        )
        artifact = build_artifact(
            run_id=run_id,
            profile=profile,
            summary=result.summary,
            findings=(),
            partial=True,
            reasons=(PartialReason.OUTPUT_INVALID,),
            now=now,
        )
        if not await self._repository.persist_artifact(
            artifact, ReviewRunState.GENERATION_ATTEMPTED
        ):
            raise RuntimeError("repair artifact could not be persisted")
        await self._repository.transition_call(
            call.identity.id, ProviderCallState.RESPONSE_OBSERVED, ProviderCallState.COMPLETED, now
        )
        return artifact
