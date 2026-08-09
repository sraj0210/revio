"""Phase 4 model-profile, gate, artifact, and marker policy."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.application.review.execution import DiffLimiter, build_artifact
from revio.application.review.markers import marker_key_id, marker_matches, review_marker
from revio.config.review import ReviewSettings
from revio.domain.models import (
    CollectionCompleteness,
    DiffCollection,
    DiffFile,
    DiffLine,
    TokenUsage,
)
from revio.domain.reviews import PartialReason, UsageDisposition
from revio.errors import UnknownModelAliasError
from revio.worker.main import resolve_review_profile


def test_sonnet_profile_is_deterministic() -> None:
    profile = anthropic_model_profile()
    assert profile.provider_model_id == "claude-sonnet-5"
    assert profile.thinking == "disabled"
    assert profile.use_default_sampling
    assert profile.structured_output == "json_schema"
    assert profile.token_counting


def test_review_gates_and_admission_margin() -> None:
    settings = ReviewSettings()
    assert settings.admission_token_ceiling == 118_000
    with pytest.raises(ValidationError, match="publishing requires review execution"):
        ReviewSettings(review_publish_enabled=True, publish_marker_key=SecretStr("x" * 32))
    with pytest.raises(ValidationError, match="forbidden in production"):
        ReviewSettings(
            environment="production",
            review_enabled=True,
            review_publish_enabled=True,
            publish_marker_key=SecretStr("x" * 32),
        )
    sandbox = ReviewSettings(
        environment="sandbox",
        review_enabled=True,
        review_publish_enabled=True,
        publish_marker_key=SecretStr("x" * 32),
    )
    assert sandbox.review_publish_enabled


def test_compose_propagates_explicit_environment_to_all_services() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
    assert "REVIO_ENVIRONMENT: ${REVIO_ENVIRONMENT:-local}" in compose
    assert "environment: &revio-environment" in compose
    assert compose.count("environment: *revio-environment") == 2


def test_model_alias_resolution_is_administrator_controlled() -> None:
    profile = resolve_review_profile(ReviewSettings(review_model_alias="review-default"))
    assert profile.provider_model_id == "claude-sonnet-5"
    with pytest.raises(UnknownModelAliasError):
        resolve_review_profile(ReviewSettings(review_model_alias="unknown"))


def test_marker_and_key_identity_are_case_preserving_and_separated() -> None:
    key = bytes(range(32))
    other = bytes(range(1, 33))
    marker = review_marker(key, "operation-1")
    assert marker == review_marker(key, "operation-1")
    assert marker != review_marker(key, "operation-2")
    assert marker != review_marker(other, "operation-1")
    assert marker_matches(marker, marker)
    assert not marker_matches(marker, marker.swapcase())
    assert marker_key_id(key) != marker_key_id(other)
    assert len(marker_key_id(key)) == 16


def test_unknown_usage_cannot_fabricate_zeros() -> None:
    assert UsageDisposition(status="unknown").usage is None
    with pytest.raises(ValidationError):
        UsageDisposition(
            status="unknown",
            usage=TokenUsage(),
        )


def test_partial_artifact_is_deterministic_and_has_no_findings() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = build_artifact(
        run_id="run",
        profile=anthropic_model_profile(),
        summary="AI review outcome could not be confirmed.",
        findings=(),
        partial=True,
        reasons=(PartialReason.PROVIDER_CALL_AMBIGUOUS,),
        now=now,
    )
    second = build_artifact(
        run_id="run",
        profile=anthropic_model_profile(),
        summary="AI review outcome could not be confirmed.",
        findings=(),
        partial=True,
        reasons=(PartialReason.PROVIDER_CALL_AMBIGUOUS,),
        now=now,
    )
    assert first.digest == second.digest
    assert first.findings == ()


def test_diff_limiter_preserves_order_and_truncates_only_at_line_boundaries() -> None:
    settings = ReviewSettings(
        review_max_files=2,
        review_max_lines=2,
        review_max_bytes=10_000,
        review_max_patch_bytes=10_000,
    )
    collection = DiffCollection(
        items=(
            DiffFile(
                new_path="first.py",
                status="added",
                lines=(
                    DiffLine(content="one", side="new", new_line=1),
                    DiffLine(content="two", side="new", new_line=2),
                    DiffLine(content="three", side="new", new_line=3),
                ),
            ),
            DiffFile(
                new_path="second.py",
                status="added",
                lines=(DiffLine(content="four", side="new", new_line=1),),
            ),
        ),
        expected_file_count=2,
    )
    files, envelope = DiffLimiter(settings).limit(collection)
    assert [item.new_path for item in files] == ["first.py"]
    assert [line.content for line in files[0].lines] == ["one", "two"]
    assert envelope.line_count == 2
    assert envelope.service_truncation_reasons == ("service_line_limit",)


def test_diff_limiter_marks_provider_and_expected_count_incompleteness() -> None:
    collection = DiffCollection(
        items=(DiffFile(new_path="a.py", status="added", patch_state="missing"),),
        expected_file_count=2,
        completeness=CollectionCompleteness(status="provider_truncated"),
    )
    files, envelope = DiffLimiter(ReviewSettings()).limit(collection)
    assert files == () and envelope.partial
    assert not envelope.provider_collection_complete and not envelope.patch_complete
    assert envelope.returned_unique_file_count == 1
