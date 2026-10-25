"""Fail-closed production-publishing enablement gate (Phase 7)."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from revio.config.review import ReviewSettings

ROLLOUT = "github:1:2"


def _base(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment": "production",
        "review_enabled": True,
        "review_publish_enabled": True,
        "review_rollout_repositories": ROLLOUT,
        "production_publish_confirmed": True,
        "publish_marker_key_file": Path("/secrets/publish-marker.key"),
    }
    values.update(overrides)
    return values


def test_all_preconditions_met_is_accepted() -> None:
    settings = ReviewSettings(**_base())  # type: ignore[arg-type]
    assert settings.review_publish_enabled is True


def test_missing_confirmation_fails_closed() -> None:
    with pytest.raises(ValidationError, match="explicit confirmation"):
        ReviewSettings(**_base(production_publish_confirmed=False))  # type: ignore[arg-type]


def test_empty_rollout_fails_closed() -> None:
    with pytest.raises(ValidationError, match="non-empty rollout allow-list"):
        ReviewSettings(**_base(review_rollout_repositories=""))  # type: ignore[arg-type]


def test_literal_marker_key_fails_closed_in_production() -> None:
    with pytest.raises(ValidationError, match="marker-key file source"):
        ReviewSettings(
            **_base(publish_marker_key_file=None, publish_marker_key=SecretStr("x" * 40))  # type: ignore[arg-type]
        )


def test_non_production_publishing_is_unaffected() -> None:
    settings = ReviewSettings(
        environment="sandbox",
        review_enabled=True,
        review_publish_enabled=True,
        publish_marker_key=SecretStr("x" * 40),
    )
    assert settings.review_publish_enabled is True
    # Sandbox needs no confirmation, rollout, or file source.
    assert settings.production_publish_confirmed is False
