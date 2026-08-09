"""Administrator-controlled adapter and model registries."""

from dataclasses import dataclass

from revio.domain.capabilities import AIProviderCapabilities, ResolvedModelProfile, SCMCapabilities
from revio.domain.identifiers import ModelAlias, ProviderId
from revio.errors import DuplicateRegistrationError, UnknownModelAliasError, UnknownProviderError
from revio.ports.ai import AIConversationGenerator, AIReviewGenerator
from revio.ports.scm import (
    ChangeRequestReadPort,
    RepositoryContentReadPort,
    ReviewPublisherPort,
    ReviewStatusPort,
    ReviewWriterPort,
    ThreadReaderPort,
    ThreadReplyPort,
    ThreadResolverPort,
)


@dataclass(frozen=True)
class SCMAdapterBundle:
    reader: ChangeRequestReadPort
    capabilities: SCMCapabilities
    repository_content: RepositoryContentReadPort | None = None
    publisher: ReviewPublisherPort | None = None
    status: ReviewStatusPort | None = None
    thread_reader: ThreadReaderPort | None = None
    thread_resolver: ThreadResolverPort | None = None
    thread_replier: ThreadReplyPort | None = None
    review_writer: ReviewWriterPort | None = None


@dataclass(frozen=True)
class AIAdapterBundle:
    reviewer: AIReviewGenerator
    capabilities: AIProviderCapabilities
    conversation: AIConversationGenerator | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._scm: dict[ProviderId, SCMAdapterBundle] = {}
        self._ai: dict[ProviderId, AIAdapterBundle] = {}

    def register_scm(self, provider_id: ProviderId, bundle: SCMAdapterBundle) -> None:
        self._register(self._scm, provider_id, bundle)

    def register_ai(self, provider_id: ProviderId, bundle: AIAdapterBundle) -> None:
        self._register(self._ai, provider_id, bundle)

    def scm(self, provider_id: ProviderId) -> SCMAdapterBundle:
        try:
            return self._scm[provider_id]
        except KeyError as error:
            raise UnknownProviderError(str(provider_id)) from error

    def ai(self, provider_id: ProviderId) -> AIAdapterBundle:
        try:
            return self._ai[provider_id]
        except KeyError as error:
            raise UnknownProviderError(str(provider_id)) from error

    @staticmethod
    def _register[T](registry: dict[ProviderId, T], provider_id: ProviderId, value: T) -> None:
        if provider_id in registry:
            raise DuplicateRegistrationError(str(provider_id))
        registry[provider_id] = value


class ModelRegistry:
    def __init__(self) -> None:
        self._profiles: dict[ModelAlias, ResolvedModelProfile] = {}

    def register(self, profile: ResolvedModelProfile) -> None:
        if profile.alias in self._profiles:
            raise DuplicateRegistrationError(profile.alias.value)
        self._profiles[profile.alias] = profile

    def resolve(self, alias: ModelAlias) -> ResolvedModelProfile:
        try:
            return self._profiles[alias]
        except KeyError as error:
            raise UnknownModelAliasError(alias.value) from error
