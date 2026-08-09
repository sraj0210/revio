"""Phase 4 model-profile, gate, artifact, and marker policy."""

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from revio.adapters.ai.anthropic import anthropic_model_profile
from revio.application.review.execution import build_artifact
from revio.application.review.markers import marker_key_id, marker_matches, review_marker
from revio.config.review import ReviewSettings
from revio.domain.models import TokenUsage
from revio.domain.reviews import PartialReason, UsageDisposition


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
