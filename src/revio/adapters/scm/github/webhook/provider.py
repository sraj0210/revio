"""GitHub implementation of the provider-neutral webhook ingress port."""

import re
from collections.abc import Mapping
from typing import Any, Literal

from revio.adapters.scm.github.auth import InstallationTokenCache
from revio.adapters.scm.github.errors import GitHubResponseError
from revio.adapters.scm.github.webhook.ingress import GitHubSandboxWebhookIngress
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.adapters.scm.github.webhook.signature import verify_signature
from revio.config.github import GitHubSettings
from revio.domain.events import WebhookNormalizationResult
from revio.errors import InvalidWebhookPayloadError
from revio.ports.persistence import DurableIngressPort

_HEADER = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validated_header(value: str | None) -> str:
    if value is None or len(value) > 128 or not _HEADER.fullmatch(value):
        raise InvalidWebhookPayloadError("invalid GitHub webhook header")
    return value


class GitHubWebhookIngressProvider:
    provider_id = "github"

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        durable: DurableIngressPort | None,
        sandbox: GitHubSandboxWebhookIngress | None,
        token_cache: InstallationTokenCache | None,
    ) -> None:
        self._settings = settings
        self._durable = durable
        self._sandbox = sandbox
        self._token_cache = token_cache

    @property
    def mode(self) -> Literal["disabled", "sandbox", "durable"]:
        return self._settings.github_webhook_mode

    @property
    def max_bytes(self) -> int:
        return self._settings.github_webhook_max_bytes

    @property
    def durable(self) -> DurableIngressPort | None:
        return self._durable

    def authenticate(self, body: bytes, headers: Mapping[str, str]) -> bool:
        secret = self._settings.github_webhook_secret
        signature = headers.get("x-hub-signature-256") or ""
        return secret is not None and verify_signature(body, signature, secret.get_secret_value())

    def extract(self, headers: Mapping[str, str]) -> tuple[str, str]:
        delivery = _validated_header(headers.get("x-github-delivery"))
        event_name = _validated_header(headers.get("x-github-event")).lower()
        return delivery, event_name

    def normalize(
        self, event_name: str, delivery: str, payload: dict[str, object]
    ) -> WebhookNormalizationResult:
        try:
            return normalize_webhook(event_name, delivery, cast_payload(payload))
        except GitHubResponseError:
            raise InvalidWebhookPayloadError("invalid GitHub webhook payload") from None

    async def sandbox_process(
        self, event_name: str, delivery: str, payload: dict[str, object]
    ) -> str:
        if self._sandbox is None:
            raise InvalidWebhookPayloadError("sandbox ingress is unavailable")
        try:
            result = await self._sandbox.process(event_name, delivery, cast_payload(payload))
        except GitHubResponseError:
            raise InvalidWebhookPayloadError("invalid GitHub webhook payload") from None
        return result.disposition

    async def on_installation_change(self, result: WebhookNormalizationResult) -> None:
        event = result.event
        if (
            self._token_cache is not None
            and event is not None
            and event.event_type == "installation"
            and event.trigger in {"suspend", "deleted"}
        ):
            await self._token_cache.invalidate(int(event.installation.external_id))


def cast_payload(payload: dict[str, object]) -> dict[str, Any]:
    return payload
