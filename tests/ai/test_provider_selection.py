"""Provider selection is alias-driven and the core never branches on provider name."""

from pathlib import Path

import pytest

from venaroq.config.review import ReviewSettings
from venaroq.errors import UnknownModelAliasError
from venaroq.worker.main import resolve_review_profile


def test_default_alias_selects_anthropic() -> None:
    profile = resolve_review_profile(ReviewSettings(review_enabled=True))
    assert profile.provider_id.value == "anthropic"
    assert profile.provider_model_id == "claude-sonnet-5"


def test_openai_alias_selects_openai() -> None:
    profile = resolve_review_profile(
        ReviewSettings(review_enabled=True, review_model_alias="openai-review-default")
    )
    assert profile.provider_id.value == "openai"
    assert profile.provider_model_id == "gpt-5.1"
    assert "diff_only" in profile.allowed_review_modes


def test_unknown_alias_is_rejected() -> None:
    with pytest.raises(UnknownModelAliasError):
        resolve_review_profile(ReviewSettings(review_enabled=True, review_model_alias="nope"))


def test_orchestration_layer_has_no_provider_name_branch() -> None:
    """Acceptance criterion: no provider identifier appears in the core review path."""
    src = Path(__file__).resolve().parents[2] / "src" / "venaroq"
    roots = (src / "application" / "review", src / "domain", src / "ports")
    offenders: list[str] = []
    for root in roots:
        for path in sorted(root.glob("*.py")):
            text = path.read_text(encoding="utf-8").lower()
            if "anthropic" in text or "openai" in text:
                offenders.append(str(path.relative_to(src)))
    assert offenders == [], f"provider name leaked into the core: {offenders}"
