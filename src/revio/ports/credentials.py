"""Provider-neutral process-local credential cache control."""

from typing import Protocol

from revio.domain.identifiers import InstallationRef


class InstallationCredentialCachePort(Protocol):
    async def invalidate(self, installation: InstallationRef) -> None: ...
