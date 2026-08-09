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
    ProviderTransientError,
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
            encoded = json.dumps(item.model_dump(mode="json"), separators=(",", ":")).encode()
            if len(encoded) > self._settings.review_max_patch_bytes:
                reasons.append("service_patch_byte_limit")
                continue
            lines = len(item.lines)
            if total_lines + lines > self._settings.review_max_lines:
                reasons.append("service_line_limit")
                break
            if total_bytes + len(encoded) > self._settings.review_max_bytes:
                reasons.append("service_byte_limit")
                break
            accepted.append(item)
            total_lines += lines
            total_bytes += len(encoded)
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
        try:
            payload = await self._reviewer.preflight(request)
        except IncompleteReviewInputError:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.INPUT_TRUNCATED,
                "Review input exceeded the approved token admission ceiling.",
                current_time,
                ReviewRunState.GENERATION_PENDING,
            )
        call_identity = ProviderCallIdentity(
            id=_id("provider-call", f"{run_id}:initial:1"),
            review_run_id=run_id,
            call_kind="initial",
            call_ordinal=1,
            provider_id=str(profile.provider_id),
            model_profile_id=profile.alias.value,
            model_profile_version=profile.profile_version,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
        )
        call = await self._repository.reserve_call(call_identity, current_time)
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
        if call.state in {ProviderCallState.AMBIGUOUS, ProviderCallState.KNOWN_REJECTED}:
            return await self._fallback(
                run_id,
                profile,
                PartialReason.PROVIDER_CALL_AMBIGUOUS,
                "AI review outcome was unavailable.",
                current_time,
            )
        if call.state == ProviderCallState.COMPLETED:
            artifact = await self._repository.get_artifact(run_id)
            if artifact is None:
                raise RuntimeError("completed provider call is missing its artifact")
            return artifact
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
            try:
                repair_payload = await self._reviewer.repair_preflight(payload)
            except (IncompleteReviewInputError, ProviderTransientError):
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.REPAIR_FAILED,
                    "AI review output could not be validated.",
                    current_time,
                )
            repair_identity = ProviderCallIdentity(
                id=_id("provider-call", f"{run_id}:repair:1"),
                review_run_id=run_id,
                call_kind="repair",
                call_ordinal=1,
                provider_id=str(profile.provider_id),
                model_profile_id=profile.alias.value,
                model_profile_version=profile.profile_version,
                prompt_version=PROMPT_VERSION,
                schema_version=SCHEMA_VERSION,
            )
            repair = await self._repository.reserve_call(repair_identity, current_time)
            if repair.state != ProviderCallState.RESERVED:
                if repair.state == ProviderCallState.ATTEMPT_STARTED:
                    await self._repository.transition_call(
                        repair.identity.id,
                        ProviderCallState.ATTEMPT_STARTED,
                        ProviderCallState.AMBIGUOUS,
                        current_time,
                        usage=UsageDisposition(status="unknown"),
                    )
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.REPAIR_FAILED,
                    "AI review repair outcome was unavailable.",
                    current_time,
                )
            if not await self._repository.transition_call(
                repair.identity.id,
                ProviderCallState.RESERVED,
                ProviderCallState.ATTEMPT_STARTED,
                current_time,
            ):
                raise RuntimeError("repair attempt could not start") from None
            try:
                repaired = await self._reviewer.generate_preflighted(repair_payload)
            except ProviderCallAmbiguousError:
                await self._repository.transition_call(
                    repair.identity.id,
                    ProviderCallState.ATTEMPT_STARTED,
                    ProviderCallState.AMBIGUOUS,
                    current_time,
                    usage=UsageDisposition(status="unknown"),
                )
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.REPAIR_FAILED,
                    "AI review repair outcome could not be confirmed.",
                    current_time,
                )
            except MalformedProviderOutputError as repair_error:
                repair_usage = repair_error.usage
                if not isinstance(repair_usage, TokenUsage):
                    raise RuntimeError("repair output lacked normalized usage") from None
                await self._repository.transition_call(
                    repair.identity.id,
                    ProviderCallState.ATTEMPT_STARTED,
                    ProviderCallState.RESPONSE_OBSERVED,
                    current_time,
                    usage=UsageDisposition(status="known", usage=repair_usage),
                )
                await self._repository.transition_call(
                    repair.identity.id,
                    ProviderCallState.RESPONSE_OBSERVED,
                    ProviderCallState.COMPLETED,
                    current_time,
                )
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.REPAIR_FAILED,
                    "AI review output could not be validated after one repair.",
                    current_time,
                )
            except ProviderCallRejectedError:
                await self._repository.transition_call(
                    repair.identity.id,
                    ProviderCallState.ATTEMPT_STARTED,
                    ProviderCallState.KNOWN_REJECTED,
                    current_time,
                )
                return await self._fallback(
                    run_id,
                    profile,
                    PartialReason.REPAIR_FAILED,
                    "AI review repair was rejected.",
                    current_time,
                )
            await self._repository.transition_call(
                repair.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.RESPONSE_OBSERVED,
                current_time,
                usage=UsageDisposition(status="known", usage=repaired.usage),
            )
            artifact = build_artifact(
                run_id=run_id,
                profile=profile,
                summary=repaired.summary,
                findings=(),
                partial=True,
                reasons=(PartialReason.OUTPUT_INVALID,),
                now=current_time,
            )
            await self._repository.persist_artifact(artifact, ReviewRunState.GENERATION_ATTEMPTED)
            await self._repository.transition_call(
                repair.identity.id,
                ProviderCallState.RESPONSE_OBSERVED,
                ProviderCallState.COMPLETED,
                current_time,
            )
            return artifact
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
        except (ProviderCallRejectedError, ProviderTransientError):
            await self._repository.transition_call(
                call.identity.id,
                ProviderCallState.ATTEMPT_STARTED,
                ProviderCallState.KNOWN_REJECTED,
                current_time,
            )
            raise
        await self._repository.transition_call(
            call.identity.id,
            ProviderCallState.ATTEMPT_STARTED,
            ProviderCallState.RESPONSE_OBSERVED,
            current_time,
            usage=UsageDisposition(status="known", usage=result.usage),
        )
        routed = tuple(
            item
            for item in result.findings
            if item.confidence >= self._settings.review_summary_confidence
        )[: self._settings.review_max_findings]
        artifact = build_artifact(
            run_id=run_id,
            profile=profile,
            summary=result.summary,
            findings=routed,
            partial=False,
            reasons=(),
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
