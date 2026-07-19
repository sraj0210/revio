"""Registry behavior tests."""

import pytest

from revio.domain.capabilities import AIProviderCapabilities, ResolvedModelProfile, SCMCapabilities
from revio.domain.identifiers import ModelAlias, ProviderId
from revio.domain.models import ChangeRequest, ReviewResult
from revio.errors import DuplicateRegistrationError, UnknownModelAliasError, UnknownProviderError
from revio.registries import AIAdapterBundle, ModelRegistry, ProviderRegistry, SCMAdapterBundle
from tests.fakes import FakeAIReviewer, FakeSCMReader


def test_provider_registry_composes_independent_adapters(change_request: ChangeRequest) -> None:
    registry = ProviderRegistry()
    scm_id, ai_id = ProviderId(value="example-scm"), ProviderId(value="example-ai")
    registry.register_scm(
        scm_id, SCMAdapterBundle(FakeSCMReader(change_request, []), SCMCapabilities())
    )
    registry.register_ai(
        ai_id, AIAdapterBundle(FakeAIReviewer(ReviewResult(summary="ok")), AIProviderCapabilities())
    )
    assert registry.scm(scm_id).reader is not None
    assert registry.ai(ai_id).reviewer is not None


def test_provider_registry_rejects_duplicates(change_request: ChangeRequest) -> None:
    registry, provider_id = ProviderRegistry(), ProviderId(value="example-scm")
    bundle = SCMAdapterBundle(FakeSCMReader(change_request, []), SCMCapabilities())
    registry.register_scm(provider_id, bundle)
    with pytest.raises(DuplicateRegistrationError):
        registry.register_scm(provider_id, bundle)
    with pytest.raises(UnknownProviderError):
        registry.ai(ProviderId(value="missing"))


def test_model_registry_resolves_only_approved_aliases(model_profile: ResolvedModelProfile) -> None:
    registry = ModelRegistry()
    registry.register(model_profile)
    assert registry.resolve(model_profile.alias) == model_profile
    with pytest.raises(UnknownModelAliasError):
        registry.resolve(ModelAlias(value="unknown"))
