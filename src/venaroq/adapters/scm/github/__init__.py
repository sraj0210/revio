"""Read-only GitHub adapter."""

from venaroq.domain.identifiers import ProviderId

GITHUB_PROVIDER_ID = ProviderId(value="github")

__all__ = ["GITHUB_PROVIDER_ID"]
