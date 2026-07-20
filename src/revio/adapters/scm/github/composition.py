"""Adapter-private GitHub construction shared by bootstrap and sandbox CLI."""

from dataclasses import dataclass
from datetime import timedelta

import httpx

from revio.adapters.scm.github.adapter import GitHubReadAdapter
from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.factory import github_adapter_bundle
from revio.config.github import GitHubSettings
from revio.registries import SCMAdapterBundle


@dataclass
class GitHubComposition:
    adapter: GitHubReadAdapter
    bundle: SCMAdapterBundle
    token_cache: InstallationTokenCache
    http: httpx.AsyncClient
    owns_http: bool

    async def close(self) -> None:
        if self.owns_http:
            await self.http.aclose()


def compose_github(
    settings: GitHubSettings, *, http: httpx.AsyncClient | None = None
) -> GitHubComposition:
    if not settings.github_enabled or settings.github_app_id is None:
        raise ValueError("GitHub adapter is not enabled")
    key = load_private_key(settings)
    cache = InstallationTokenCache(
        timedelta(seconds=settings.github_token_refresh_margin_seconds),
        timedelta(seconds=settings.github_token_minimum_usable_lifetime_seconds),
    )
    owns_http = http is None
    timeout = settings.github_http_timeout_seconds
    client_http = http or httpx.AsyncClient(
        base_url=settings.github_api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.github_api_version,
            "User-Agent": "revio-phase2",
        },
        timeout=httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout),
        follow_redirects=False,
    )
    client = GitHubClient(
        client_http,
        GitHubAppJWT(settings.github_app_id, key),
        cache,
        api_url=settings.github_api_url,
    )
    adapter = GitHubReadAdapter(
        client,
        max_pages=settings.github_max_pages,
        max_items=settings.github_max_items,
    )
    return GitHubComposition(adapter, github_adapter_bundle(adapter), cache, client_http, owns_http)
