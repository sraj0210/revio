"""Adapter-private GitHub construction shared by bootstrap and sandbox CLI."""

from dataclasses import dataclass
from datetime import timedelta

import httpx

from venaroq.adapters.scm.github.adapter import GitHubReadAdapter
from venaroq.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from venaroq.adapters.scm.github.client import GitHubClient
from venaroq.adapters.scm.github.factory import github_adapter_bundle
from venaroq.adapters.scm.github.publishing import GitHubReviewWriter
from venaroq.adapters.scm.github.threads import GitHubThreadReader, GitHubThreadResolver
from venaroq.config.github import GitHubSettings
from venaroq.config.review import ReviewSettings
from venaroq.registries import SCMAdapterBundle


@dataclass
class GitHubComposition:
    adapter: GitHubReadAdapter
    bundle: SCMAdapterBundle
    token_cache: InstallationTokenCache
    http: httpx.AsyncClient
    owns_http: bool
    client: GitHubClient
    writer: GitHubReviewWriter | None = None
    thread_reader: GitHubThreadReader | None = None
    thread_resolver: GitHubThreadResolver | None = None

    async def close(self) -> None:
        if self.owns_http:
            await self.http.aclose()


def compose_github(
    settings: GitHubSettings,
    *,
    http: httpx.AsyncClient | None = None,
    review_settings: ReviewSettings | None = None,
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
            "User-Agent": "venaroq-phase2",
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
    publish_enabled = review_settings is not None and review_settings.review_publish_enabled
    writer = (
        GitHubReviewWriter(
            client,
            settings.github_app_id,
            max_pages=settings.github_max_pages,
            max_items=settings.github_max_items,
        )
        if publish_enabled
        else None
    )
    thread_reader = (
        GitHubThreadReader(
            client, max_pages=settings.github_max_pages, max_items=settings.github_max_items
        )
        if publish_enabled
        else None
    )
    thread_resolver = GitHubThreadResolver(client) if publish_enabled else None
    return GitHubComposition(
        adapter,
        github_adapter_bundle(adapter, writer, thread_reader, thread_resolver),
        cache,
        client_http,
        owns_http,
        client,
        writer,
        thread_reader,
        thread_resolver,
    )
