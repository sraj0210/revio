"""Authenticated GitHub client origin, retry, and error tests."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationTokenCache, load_private_key
from revio.adapters.scm.github.client import GitHubClient
from revio.adapters.scm.github.errors import (
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubProviderUnavailableError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubTransportError,
    GitHubValidationError,
)
from revio.config.github import GitHubSettings


def settings(pem: str) -> GitHubSettings:
    return GitHubSettings(github_enabled=True, github_app_id=1, github_private_key=SecretStr(pem))


def token_response(value: object = "opaque") -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "token": value,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )


def client_for(
    pem: str, handler: httpx.AsyncBaseTransport
) -> tuple[GitHubClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        base_url="https://api.github.com", transport=handler, follow_redirects=False
    )
    client = GitHubClient(
        http,
        GitHubAppJWT(1, load_private_key(settings(pem))),
        InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=30)),
    )
    return client, http


@pytest.mark.asyncio
async def test_401_invalidates_refreshes_and_retries_once(rsa_private_key_pem: str) -> None:
    token_calls = read_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls, read_calls
        if request.url.path.endswith("access_tokens"):
            token_calls += 1
            return token_response(f"token-{token_calls}")
        read_calls += 1
        return httpx.Response(401 if read_calls == 1 else 200, json={"ok": True})

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        assert (await client.get(9, "/resource")).status_code == 200
    assert (token_calls, read_calls) == (2, 2)


@pytest.mark.asyncio
async def test_second_401_does_not_loop(rsa_private_key_pem: str) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("access_tokens"):
            return token_response()
        calls += 1
        return httpx.Response(401)

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubAuthenticationError):
            await client.get(9, "/resource")
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["https://evil.invalid/steal", "//evil.invalid/steal"])
async def test_absolute_and_scheme_relative_urls_are_rejected_before_auth(
    rsa_private_key_pem: str, path: str
) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return token_response()

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubResponseError):
            await client.get(9, path)
    assert calls == 0


@pytest.mark.asyncio
async def test_same_origin_redirect_is_followed_without_auth_retry(
    rsa_private_key_pem: str,
) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("access_tokens"):
            return token_response()
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new"})
        return httpx.Response(200)

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        await client.get(9, "/old")
    reads = [request for request in seen if not request.url.path.endswith("access_tokens")]
    assert [request.url.path for request in reads] == ["/old", "/new"]
    assert all(request.headers.get("Authorization") == "Bearer opaque" for request in reads)


@pytest.mark.asyncio
async def test_cross_origin_redirect_is_rejected_and_token_never_reaches_foreign_host(
    rsa_private_key_pem: str,
) -> None:
    foreign_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal foreign_calls
        if request.url.host != "api.github.com":
            foreign_calls += 1
        if request.url.path.endswith("access_tokens"):
            return token_response()
        return httpx.Response(302, headers={"Location": "https://evil.invalid/steal"})

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubResponseError):
            await client.get(9, "/old")
    assert foreign_calls == 0


def test_pagination_links_are_origin_confined(rsa_private_key_pem: str) -> None:
    client, http = client_for(
        rsa_private_key_pem, httpx.MockTransport(lambda _: httpx.Response(200))
    )
    assert client.pagination_url("https://api.github.com/next?page=2") == "/next?page=2"
    with pytest.raises(GitHubResponseError):
        client.pagination_url("https://evil.invalid/next")
    # Close the unstarted client transport explicitly.
    import asyncio

    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    "url",
    [
        "https://user@api.github.com/next",
        "https://api.github.com:444/next",
        "https://api.github.com./next",
        "https://[broken/next",
        "not a url",
    ],
)
def test_pagination_rejects_userinfo_alternate_origins_and_malformed_urls(
    rsa_private_key_pem: str, url: str
) -> None:
    client, http = client_for(
        rsa_private_key_pem, httpx.MockTransport(lambda _: httpx.Response(200))
    )
    with pytest.raises(GitHubResponseError):
        client.pagination_url(url)
    import asyncio

    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    ("status", "headers", "body", "error_type"),
    [
        (401, {}, None, GitHubAuthenticationError),
        (403, {}, None, GitHubPermissionError),
        (404, {}, None, GitHubNotFoundError),
        (422, {}, None, GitHubValidationError),
        (429, {"Retry-After": "2"}, None, GitHubRateLimitedError),
        (500, {}, None, GitHubProviderUnavailableError),
        (503, {"Retry-After": "4"}, None, GitHubProviderUnavailableError),
    ],
)
def test_http_status_classification(
    status: int,
    headers: dict[str, str],
    body: object,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        GitHubClient.raise_for_response(httpx.Response(status, headers=headers, json=body))


def test_primary_and_secondary_rate_metadata() -> None:
    with pytest.raises(GitHubRateLimitedError) as primary:
        GitHubClient.raise_for_response(
            httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"},
            )
        )
    assert (primary.value.kind, primary.value.reset_epoch) == ("primary", 123)
    with pytest.raises(GitHubRateLimitedError) as secondary:
        GitHubClient.raise_for_response(
            httpx.Response(403, json={"message": "secondary rate limit"})
        )
    assert secondary.value.kind == "secondary"


@pytest.mark.asyncio
async def test_transport_timeout_is_typed(rsa_private_key_pem: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_tokens"):
            return token_response()
        raise httpx.ReadTimeout("unsafe-provider-detail", request=request)

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubTransportError) as caught:
            await client.get(9, "/resource")
    assert "unsafe-provider-detail" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timeout_type", [httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
async def test_all_transport_timeout_phases_are_safely_normalized(
    rsa_private_key_pem: str, timeout_type: type[httpx.TimeoutException]
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_tokens"):
            return token_response()
        raise timeout_type("SENTINEL-TIMEOUT-DETAIL", request=request)

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubTransportError) as caught:
            await client.get(9, "/resource?credential=SENTINEL-QUERY")
    rendered = f"{caught.value!s} {caught.value!r} {caught.value.__context__}"
    assert "SENTINEL" not in rendered


@pytest.mark.asyncio
async def test_malformed_token_response_has_no_secret_exception_context(
    rsa_private_key_pem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SENTINEL-PROVIDER-SECRET"

    async def handler(_: httpx.Request) -> httpx.Response:
        return token_response({"unsafe": sentinel})

    client, http = client_for(rsa_private_key_pem, httpx.MockTransport(handler))
    async with http:
        with pytest.raises(GitHubAuthenticationError) as caught:
            await client.get(9, "/resource")
    rendered = " ".join(
        map(
            str,
            [caught.value, repr(caught.value), caught.value.__cause__, caught.value.__context__],
        )
    )
    assert sentinel not in rendered
    assert sentinel not in caplog.text


def test_503_preserves_retry_metadata() -> None:
    with pytest.raises(GitHubProviderUnavailableError) as caught:
        GitHubClient.raise_for_response(httpx.Response(503, headers={"Retry-After": "4"}))
    assert caught.value.retry_after_seconds == 4
