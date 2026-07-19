"""Shared Phase 1 fixtures."""

import pytest

from revio.domain.capabilities import ResolvedModelProfile
from revio.domain.identifiers import (
    ChangeRequestTarget,
    InstallationRef,
    ModelAlias,
    ProviderId,
    RepositoryRef,
)
from revio.domain.models import ChangeRequest


@pytest.fixture
def target() -> ChangeRequestTarget:
    provider = ProviderId(value="example-scm")
    installation = InstallationRef(provider_id=provider, external_id="installation-1")
    repository = RepositoryRef(installation=installation, external_id="repository-1")
    return ChangeRequestTarget(repository=repository, external_number=7)


@pytest.fixture
def change_request(target: ChangeRequestTarget) -> ChangeRequest:
    return ChangeRequest(target=target, title="Change", base_sha="base", head_sha="head")


@pytest.fixture
def model_profile() -> ResolvedModelProfile:
    return ResolvedModelProfile(
        alias=ModelAlias(value="review-default"),
        provider_id=ProviderId(value="example-ai"),
        provider_model_id="model-1",
        context_tokens=100_000,
        max_output_tokens=4_000,
        structured_output="json_schema",
    )
