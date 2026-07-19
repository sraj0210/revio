"""Authenticated GitHub client retry and error tests."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr

from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import (
    GitHubAuthenticationError,
    GitHubRateLimitedError,
    GitHubTransportError,
)
from revio.config.github import GitHubSettings


@pytest.mark.asyncio
@respx.mock
async def test_401_invalidates_refreshes_and_retries_once(rsa_private_key_pem: str) -> None:
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    token_route = respx.post("https://api.github.com/app/installations/9/access_tokens").mock(
        side_effect=[
            httpx.Response(201, json={"token": "token-1", "expires_at": expires_at}),
            httpx.Response(201, json={"token": "token-2", "expires_at": expires_at}),
        ]
    )
    read_route = respx.get("https://api.github.com/resource").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )

    settings = GitHubSettings(
        github_enabled=True, github_app_id=1, github_private_key=SecretStr(rsa_private_key_pem)
    )
    async with httpx.AsyncClient(base_url="https://api.github.com") as http:
        client = GitHubClient(
            http,
            GitHubAppJWT(1, load_private_key(settings)),
            InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30)),
        )
        assert (await client.get(9, "/resource")).status_code == 200
    assert token_route.call_count == 2
    assert read_route.call_count == 2


@pytest.mark.asyncio
async def test_second_401_does_not_loop(rsa_private_key_pem: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "opaque",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        calls += 1
        return httpx.Response(401)

    settings = GitHubSettings(
        github_enabled=True, github_app_id=1, github_private_key=SecretStr(rsa_private_key_pem)
    )
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http:
        client = GitHubClient(
            http,
            GitHubAppJWT(1, load_private_key(settings)),
            InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30)),
        )
        with pytest.raises(GitHubAuthenticationError):
            await client.get(9, "/resource")
    assert calls == 2


def test_rate_limit_is_typed() -> None:
    with pytest.raises(GitHubRateLimitedError) as caught:
        GitHubClient.raise_for_response(httpx.Response(429, headers={"Retry-After": "2"}))
    assert caught.value.retry_after_seconds == 2


def test_secondary_rate_limit_is_typed_without_retry_header() -> None:
    with pytest.raises(GitHubRateLimitedError):
        GitHubClient.raise_for_response(
            httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"})
        )


@pytest.mark.asyncio
async def test_transport_failure_is_typed_and_redacted(rsa_private_key_pem: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "highly-secret-token",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        raise httpx.ReadTimeout("transport included no credentials", request=request)

    settings = GitHubSettings(
        github_enabled=True, github_app_id=1, github_private_key=SecretStr(rsa_private_key_pem)
    )
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http:
        client = GitHubClient(
            http,
            GitHubAppJWT(1, load_private_key(settings)),
            InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30)),
        )
        with pytest.raises(GitHubTransportError) as caught:
            await client.get(9, "/resource")
    assert "highly-secret-token" not in str(caught.value)
