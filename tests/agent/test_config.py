"""Agent-mode configuration bounds and cross-validation."""

import pytest
from pydantic import ValidationError

from venaroq.config.review import ReviewSettings


def test_per_file_byte_limit_is_capped_to_the_tool_result_bound() -> None:
    # The DTO caps tool-result content at 200_000; the config must not admit a larger file.
    with pytest.raises(ValidationError):
        ReviewSettings(review_enabled=True, review_agent_max_bytes_per_file=500_000)


def test_per_file_cannot_exceed_aggregate() -> None:
    with pytest.raises(ValidationError, match="per-file byte limit cannot exceed"):
        ReviewSettings(
            review_enabled=True,
            review_agent_max_bytes_per_file=200_000,
            review_agent_max_aggregate_bytes=100_000,
        )


def test_agent_defaults_are_off_and_sane() -> None:
    settings = ReviewSettings()
    assert settings.review_agent_mode_enabled is False
    assert settings.review_agent_max_bytes_per_file <= settings.review_agent_max_aggregate_bytes
