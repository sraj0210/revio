"""GitHub App authentication and installation-token caching."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from pydantic import SecretStr

from revio.adapters.scm.github.errors import GitHubAuthenticationError, GitHubConfigurationError
from revio.config.github import GitHubSettings


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def load_private_key(settings: GitHubSettings) -> RSAPrivateKey:
    if settings.github_private_key is not None:
        raw = settings.github_private_key.get_secret_value().encode()
    elif settings.github_private_key_file is not None:
        path = settings.github_private_key_file
        if not path.is_absolute():
            raise GitHubConfigurationError("GitHub private-key path must be absolute")
        try:
            with path.open("rb") as key_file:
                raw = key_file.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise GitHubConfigurationError("GitHub private-key file is too large")
        except OSError:
            raise GitHubConfigurationError("unable to read GitHub private key") from None
    else:
        raise GitHubConfigurationError("GitHub private key is not configured")
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError):
        raise GitHubConfigurationError("invalid GitHub private key") from None
    if not isinstance(key, RSAPrivateKey):
        raise GitHubConfigurationError("GitHub private key must be RSA")
    return key


class GitHubAppJWT:
    def __init__(self, app_id: int, key: RSAPrivateKey, clock: Clock | None = None) -> None:
        self._app_id, self._key, self._clock = app_id, key, clock or SystemClock()

    def create(self) -> str:
        now = self._clock.now()
        return jwt.encode(
            {
                "iat": now - timedelta(seconds=60),
                "exp": now + timedelta(minutes=9),
                "iss": str(self._app_id),
            },
            self._key,
            algorithm="RS256",
        )


@dataclass(frozen=True)
class InstallationToken:
    secret: SecretStr
    expires_at: datetime


RefreshToken = Callable[[int], Awaitable[InstallationToken]]


class InstallationTokenCache:
    """Process-local cache retaining stable lock identity across invalidation."""

    def __init__(
        self, refresh_margin: timedelta, minimum_usable: timedelta, clock: Clock | None = None
    ) -> None:
        self._refresh_margin, self._minimum_usable = refresh_margin, minimum_usable
        self._clock = clock or SystemClock()
        self._tokens: dict[int, InstallationToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, installation_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(installation_id, asyncio.Lock())

    async def get(self, installation_id: int, refresh: RefreshToken) -> SecretStr:
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - self._clock.now() > self._refresh_margin:
            return cached.secret
        lock = await self._lock_for(installation_id)
        async with lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - self._clock.now() > self._refresh_margin:
                return cached.secret
            try:
                token = await refresh(installation_id)
            except Exception as error:
                if cached and cached.expires_at - self._clock.now() > self._minimum_usable:
                    return cached.secret
                raise GitHubAuthenticationError("unable to refresh installation token") from error
            if token.expires_at <= self._clock.now():
                raise GitHubAuthenticationError("provider returned an expired installation token")
            self._tokens[installation_id] = token
            return token.secret

    async def invalidate(self, installation_id: int) -> None:
        lock = await self._lock_for(installation_id)
        async with lock:
            self._tokens.pop(installation_id, None)
        # Locks intentionally remain stable; removing one could create a split-lock race.
