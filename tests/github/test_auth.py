"""GitHub App JWT and token-cache tests."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from pydantic import SecretStr

from revio.adapters.scm.github.auth import (
    GitHubAppJWT,
    InstallationToken,
    InstallationTokenCache,
    load_private_key,
)
from revio.adapters.scm.github.errors import GitHubAuthenticationError
from revio.config.github import GitHubSettings


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def test_jwt_has_expected_claims(rsa_private_key_pem: str) -> None:
    clock = FakeClock()
    settings = GitHubSettings(
        github_enabled=True, github_app_id=123, github_private_key=SecretStr(rsa_private_key_pem)
    )
    token = GitHubAppJWT(123, load_private_key(settings), clock).create()
    claims = jwt.decode(
        token,
        load_private_key(settings).public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False},
    )
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] == 600


def test_private_key_loads_from_absolute_secret_file(
    rsa_private_key_pem: str, tmp_path: Path
) -> None:
    path = tmp_path / "github.pem"
    path.write_text(rsa_private_key_pem)
    settings = GitHubSettings(github_enabled=True, github_app_id=1, github_private_key_file=path)
    assert load_private_key(settings).key_size == 2048


@pytest.mark.asyncio
async def test_cache_refresh_margin_and_minimum_usable_lifetime() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=20), clock)
    calls = 0

    async def refresh(_: int) -> InstallationToken:
        nonlocal calls
        calls += 1
        return InstallationToken(SecretStr(f"token-{calls}"), clock.now() + timedelta(seconds=90))

    assert (await cache.get(1, refresh)).get_secret_value() == "token-1"
    clock.value += timedelta(seconds=31)
    assert (await cache.get(1, refresh)).get_secret_value() == "token-2"
    assert calls == 2

    async def fail(_: int) -> InstallationToken:
        raise RuntimeError

    clock.value += timedelta(seconds=55)
    assert (await cache.get(1, fail)).get_secret_value() == "token-2"
    clock.value += timedelta(seconds=16)
    with pytest.raises(GitHubAuthenticationError):
        await cache.get(1, fail)


@pytest.mark.asyncio
async def test_invalidation_during_refresh_does_not_split_lock() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=10), clock)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def refresh(_: int) -> InstallationToken:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return InstallationToken(SecretStr(f"token-{calls}"), clock.now() + timedelta(hours=1))

    first = asyncio.create_task(cache.get(7, refresh))
    await started.wait()
    invalidation = asyncio.create_task(cache.invalidate(7))
    await asyncio.sleep(0)
    assert not invalidation.done()
    release.set()
    await first
    await invalidation
    await cache.get(7, refresh)
    assert calls == 2
