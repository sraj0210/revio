"""Provider-neutral cache invalidation facade for the GitHub token cache."""

from venaroq.adapters.scm.github.auth import InstallationTokenCache
from venaroq.domain.identifiers import InstallationRef


class GitHubCredentialCache:
    def __init__(self, cache: InstallationTokenCache) -> None:
        self._cache = cache

    async def invalidate(self, installation: InstallationRef) -> None:
        await self._cache.invalidate(int(installation.external_id))
