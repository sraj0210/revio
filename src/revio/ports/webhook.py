"""Provider-neutral webhook ingress port.

Each SCM provider supplies its own authentication, header extraction, normalization, and
installation-lifecycle hook; the generic ingress route owns the shared flow (routing, media-type and
byte limits, rate limiting, durable persistence, and the fail-closed exception mapping). This lets
any registered SCM provider be wired into the same ingress without provider conditionals in the
route.
"""

from collections.abc import Mapping
from typing import Literal, Protocol

from revio.domain.events import WebhookNormalizationResult
from revio.ports.persistence import DurableIngressPort


class WebhookIngressProvider(Protocol):
    @property
    def provider_id(self) -> str: ...
    @property
    def mode(self) -> Literal["disabled", "sandbox", "durable"]: ...
    @property
    def max_bytes(self) -> int: ...
    @property
    def durable(self) -> DurableIngressPort | None: ...
    def authenticate(self, body: bytes, headers: Mapping[str, str]) -> bool: ...
    def extract(self, headers: Mapping[str, str]) -> tuple[str, str]:
        """Return (delivery, event_name); raise InvalidWebhookPayloadError when invalid."""
        ...

    def normalize(
        self, event_name: str, delivery: str, payload: dict[str, object]
    ) -> WebhookNormalizationResult: ...
    async def sandbox_process(
        self, event_name: str, delivery: str, payload: dict[str, object]
    ) -> str: ...
    async def on_installation_change(self, result: WebhookNormalizationResult) -> None: ...
