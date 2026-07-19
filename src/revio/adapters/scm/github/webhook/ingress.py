"""GitHub-private non-durable webhook ingress coordination."""

from typing import Any

from revio.adapters.scm.github.auth import InstallationTokenCache
from revio.adapters.scm.github.webhook.normalizer import normalize_webhook
from revio.domain.events import WebhookNormalizationResult


class GitHubSandboxWebhookIngress:
    def __init__(self, cache: InstallationTokenCache) -> None:
        self._cache = cache

    async def process(
        self, event_name: str, delivery_id: str, payload: dict[str, Any]
    ) -> WebhookNormalizationResult:
        result = normalize_webhook(event_name, delivery_id, payload)
        if (
            result.event
            and result.event.event_type == "installation"
            and result.event.trigger in {"deleted", "suspend"}
        ):
            await self._cache.invalidate(int(result.event.installation.external_id))
        return result
