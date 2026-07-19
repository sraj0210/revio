"""Domain validation tests."""

import pytest
from pydantic import ValidationError

from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import ModelAlias, ProviderId


def test_provider_id_is_extensible_and_validated() -> None:
    assert str(ProviderId(value="gitlab-self-managed")) == "gitlab-self-managed"
    with pytest.raises(ValidationError):
        ProviderId(value="GitHub")


def test_model_capabilities_are_resolved_per_alias() -> None:
    profile = ResolvedModelProfile(
        alias=ModelAlias(value="large-review"),
        provider_id=ProviderId(value="example-ai"),
        provider_model_id="opaque-model",
        context_tokens=200_000,
        max_output_tokens=8_000,
        structured_output="json_schema",
        native_tools=True,
        prompt_caching=True,
    )
    assert profile.context_tokens == 200_000
    assert profile.native_tools is True


def test_model_profile_rejects_invalid_limits() -> None:
    with pytest.raises(ValidationError):
        ResolvedModelProfile(
            alias=ModelAlias(value="bad"),
            provider_id=ProviderId(value="example-ai"),
            provider_model_id="model",
            context_tokens=0,
            max_output_tokens=1,
        )
