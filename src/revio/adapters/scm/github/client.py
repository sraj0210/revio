"""Authenticated read-only GitHub REST client."""

from typing import Any

import httpx

from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationToken, InstallationTokenCache
from revio.adapters.scm.github.dto.api import GitHubTokenDTO
from revio.adapters.scm.github.errors import (
    GitHubAuthenticationError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubTransportError,
)


class GitHubClient:
    def __init__(
        self, http: httpx.AsyncClient, app_jwt: GitHubAppJWT, cache: InstallationTokenCache
    ) -> None:
        self._http, self._app_jwt, self._cache = http, app_jwt, cache

    async def _refresh(self, installation_id: int) -> InstallationToken:
        try:
            response = await self._http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._app_jwt.create()}"},
            )
        except httpx.HTTPError as error:
            raise GitHubTransportError("GitHub token request failed") from error
        self.raise_for_response(response)
        try:
            dto = GitHubTokenDTO.model_validate(response.json())
        except (ValueError, TypeError) as error:
            raise GitHubResponseError("invalid GitHub token response") from error
        from pydantic import SecretStr

        return InstallationToken(SecretStr(dto.token), dto.expires_at)

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        for attempt in range(2):
            token = await self._cache.get(installation_id, self._refresh)
            try:
                response = await self._http.get(
                    path,
                    params=params,
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                )
            except httpx.HTTPError as error:
                raise GitHubTransportError("GitHub read request failed") from error
            if response.status_code != 401 or attempt == 1:
                self.raise_for_response(response)
                return response
            await self._cache.invalidate(installation_id)
        raise GitHubAuthenticationError("GitHub authentication failed")

    @staticmethod
    def raise_for_response(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 401:
            raise GitHubAuthenticationError("GitHub authentication failed")
        if response.status_code == 404:
            raise GitHubNotFoundError("GitHub resource not found")
        if response.status_code in (403, 429):
            retry = response.headers.get("Retry-After")
            try:
                message = str(response.json().get("message", "")).lower()
            except (ValueError, AttributeError):
                message = ""
            limited = (
                response.status_code == 429
                or retry is not None
                or response.headers.get("X-RateLimit-Remaining") == "0"
                or "secondary rate limit" in message
            )
            if limited:
                try:
                    seconds = float(retry) if retry else None
                except ValueError:
                    seconds = None
                raise GitHubRateLimitedError(seconds)
            raise GitHubPermissionError("GitHub permission denied")
        raise GitHubResponseError(f"GitHub provider returned HTTP {response.status_code}")
