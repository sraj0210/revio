"""GitHub App JWT and token-cache tests."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from revio.adapters.scm.github.auth import (
    GitHubAppJWT,
    InstallationToken,
    InstallationTokenCache,
    load_private_key,
)
from revio.adapters.scm.github.errors import (
    GitHubAuthenticationError,
    GitHubConfigurationError,
)
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


@pytest.mark.asyncio
async def test_same_installation_refresh_is_single_flight() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=10), clock)
    calls = 0

    async def refresh(_: int) -> InstallationToken:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return InstallationToken(SecretStr("token"), clock.now() + timedelta(hours=1))

    results = await asyncio.gather(*(cache.get(7, refresh) for _ in range(10)))
    assert calls == 1
    assert {item.get_secret_value() for item in results} == {"token"}


@pytest.mark.asyncio
async def test_different_installations_refresh_concurrently() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=10), clock)
    both_started = asyncio.Event()
    started: set[int] = set()

    async def refresh(installation: int) -> InstallationToken:
        started.add(installation)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return InstallationToken(SecretStr(str(installation)), clock.now() + timedelta(hours=1))

    await asyncio.gather(cache.get(1, refresh), cache.get(2, refresh))
    assert started == {1, 2}


@pytest.mark.asyncio
async def test_cancelled_refresh_does_not_poison_cache_or_lock() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=10), clock)
    started = asyncio.Event()

    async def blocked(_: int) -> InstallationToken:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(cache.get(7, blocked))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def successful(_: int) -> InstallationToken:
        return InstallationToken(SecretStr("recovered"), clock.now() + timedelta(hours=1))

    assert (await cache.get(7, successful)).get_secret_value() == "recovered"


@pytest.mark.asyncio
async def test_expired_refresh_token_is_never_returned() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(seconds=60), timedelta(seconds=10), clock)

    async def refresh(_: int) -> InstallationToken:
        return InstallationToken(SecretStr("expired"), clock.now())

    with pytest.raises(GitHubAuthenticationError):
        await cache.get(1, refresh)


@pytest.mark.asyncio
async def test_refresh_failure_at_exact_minimum_lifetime_is_rejected() -> None:
    clock = FakeClock()
    cache = InstallationTokenCache(timedelta(hours=2), timedelta(seconds=30), clock)

    async def initial(_: int) -> InstallationToken:
        return InstallationToken(SecretStr("cached"), clock.now() + timedelta(seconds=30))

    await cache.get(1, initial)

    async def fail(_: int) -> InstallationToken:
        raise RuntimeError

    with pytest.raises(GitHubAuthenticationError):
        await cache.get(1, fail)


def test_private_key_file_rejects_relative_invalid_non_rsa_and_oversized(
    tmp_path: Path,
) -> None:
    relative = GitHubSettings(
        github_enabled=True, github_app_id=1, github_private_key_file=Path("relative.pem")
    )
    with pytest.raises(GitHubConfigurationError):
        load_private_key(relative)

    invalid_path = tmp_path / "invalid.pem"
    invalid_path.write_text("not a key")
    with pytest.raises(GitHubConfigurationError):
        load_private_key(
            GitHubSettings(
                github_enabled=True, github_app_id=1, github_private_key_file=invalid_path
            )
        )

    ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    ec_path = tmp_path / "ec.pem"
    ec_path.write_bytes(ec_key)
    with pytest.raises(GitHubConfigurationError):
        load_private_key(
            GitHubSettings(github_enabled=True, github_app_id=1, github_private_key_file=ec_path)
        )

    large_path = tmp_path / "large.pem"
    large_path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(GitHubConfigurationError, match="too large"):
        load_private_key(
            GitHubSettings(github_enabled=True, github_app_id=1, github_private_key_file=large_path)
        )
