"""Authenticated, origin-confined GitHub REST client."""

from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from revio.adapters.scm.github.auth import GitHubAppJWT, InstallationToken, InstallationTokenCache
from revio.adapters.scm.github.dto.api import GitHubTokenDTO
from revio.adapters.scm.github.errors import (
    GitHubAnchorValidationRejectedError,
    GitHubAuthenticationError,
    GitHubConfigurationError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubProviderUnavailableError,
    GitHubRateLimitedError,
    GitHubResponseError,
    GitHubTransportError,
    GitHubValidationError,
)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


class GitHubClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        app_jwt: GitHubAppJWT,
        cache: InstallationTokenCache,
        *,
        api_url: str = "https://api.github.com",
        max_redirects: int = 3,
    ) -> None:
        self._http, self._app_jwt, self._cache = http, app_jwt, cache
        self._api_url = httpx.URL(api_url)
        self._max_redirects = max_redirects
        if _origin(http.base_url) != _origin(self._api_url):
            raise GitHubConfigurationError("GitHub HTTP client origin is invalid")

    def relative_url(self, value: str) -> str:
        """Validate a relative API URL and retain query parameters."""
        split = urlsplit(value)
        if split.scheme or split.netloc or value.startswith("//") or not split.path.startswith("/"):
            raise GitHubResponseError("invalid GitHub API path")
        resolved = self._api_url.join(value)
        if _origin(resolved) != _origin(self._api_url):
            raise GitHubResponseError("GitHub API origin mismatch")
        return str(resolved.copy_with(scheme=None, host=None, port=None))

    def pagination_url(self, value: str) -> str:
        try:
            candidate = httpx.URL(value)
        except httpx.InvalidURL:
            raise GitHubResponseError("invalid GitHub pagination link") from None
        if candidate.userinfo or _origin(candidate) != _origin(self._api_url):
            raise GitHubResponseError("foreign GitHub pagination origin")
        return str(candidate.copy_with(scheme=None, host=None, port=None))

    async def _refresh(self, installation_id: int) -> InstallationToken:
        path = self.relative_url(f"/app/installations/{installation_id}/access_tokens")
        failure: str | None = None
        response: httpx.Response | None = None
        try:
            response = await self._http.post(
                path,
                headers={"Authorization": f"Bearer {self._app_jwt.create()}"},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            failure = "GitHub token request timed out"
        except httpx.HTTPError:
            failure = "GitHub token request failed"
        if failure is not None:
            raise GitHubTransportError(failure)
        assert response is not None
        self.raise_for_response(response)
        dto = self._parse_token(response)
        if dto is None:
            raise GitHubResponseError("invalid GitHub token response") from None
        return InstallationToken(dto.token, dto.expires_at)

    @staticmethod
    def _parse_token(response: httpx.Response) -> GitHubTokenDTO | None:
        try:
            return GitHubTokenDTO.model_validate(response.json())
        except (ValidationError, ValueError, TypeError):
            return None

    async def get(
        self, installation_id: int, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        current_path = self.relative_url(path)
        for auth_attempt in range(2):
            token = await self._cache.get(installation_id, self._refresh)
            redirects = 0
            while True:
                failure = None
                invalid_redirect = False
                response = None
                try:
                    response = await self._http.get(
                        current_path,
                        params=params,
                        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                        follow_redirects=False,
                    )
                except (httpx.InvalidURL, httpx.RemoteProtocolError):
                    invalid_redirect = True
                except httpx.TimeoutException:
                    failure = "GitHub read request timed out"
                except httpx.HTTPError:
                    failure = "GitHub read request failed"
                if invalid_redirect:
                    raise GitHubResponseError("invalid GitHub redirect")
                if failure is not None:
                    raise GitHubTransportError(failure)
                assert response is not None
                if response.status_code not in {301, 302, 307, 308}:
                    break
                location = response.headers.get("Location")
                if not location or redirects >= self._max_redirects:
                    raise GitHubResponseError("invalid GitHub redirect")
                try:
                    target = response.request.url.join(location)
                except httpx.InvalidURL:
                    target = None
                if target is None:
                    raise GitHubResponseError("invalid GitHub redirect")
                if _origin(target) != _origin(self._api_url):
                    raise GitHubResponseError("foreign GitHub redirect rejected")
                current_path = str(target.copy_with(scheme=None, host=None, port=None))
                params = None
                redirects += 1
            if response.status_code != 401 or auth_attempt == 1:
                self.raise_for_response(response)
                return response
            await self._cache.invalidate(installation_id)
        raise GitHubAuthenticationError("GitHub authentication failed")

    async def write(
        self,
        installation_id: int,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
    ) -> httpx.Response:
        """Perform one non-redirected write; transport uncertainty remains explicit."""
        from revio.adapters.scm.github.errors import GitHubAmbiguousWriteError

        current_path = self.relative_url(path)
        token = await self._cache.get(installation_id, self._refresh)
        try:
            response = await self._http.request(
                method,
                current_path,
                json=json_body,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx.HTTPError):
            raise GitHubAmbiguousWriteError("GitHub write outcome is ambiguous") from None
        if response.status_code in {301, 302, 307, 308, 401} or response.status_code >= 500:
            raise GitHubAmbiguousWriteError("GitHub write response is ambiguous")
        if (
            response.status_code == 422
            and method == "POST"
            and current_path.endswith("/reviews")
            and self._is_anchor_validation(response)
        ):
            raise GitHubAnchorValidationRejectedError(
                "GitHub rejected an inline review-comment anchor"
            )
        self.raise_for_response(response)
        return response

    @staticmethod
    def _is_anchor_validation(response: httpx.Response) -> bool:
        try:
            body = cast(object, response.json())
        except (ValueError, TypeError):
            return False
        if not isinstance(body, dict):
            return False
        errors = cast(dict[object, object], body).get("errors")
        if not isinstance(errors, list):
            return False
        error_items = cast(list[object], errors)
        if len(error_items) > 100:
            return False
        anchor_fields = {"line", "side", "path", "position", "commit_id"}
        for item in error_items:
            if not isinstance(item, dict):
                continue
            error = cast(dict[object, object], item)
            if (
                error.get("resource") == "PullRequestReviewComment"
                and error.get("field") in anchor_fields
            ):
                return True
        return False

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _reset_epoch(response: httpx.Response) -> int | None:
        value = response.headers.get("X-RateLimit-Reset")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @classmethod
    def raise_for_response(cls, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 401:
            raise GitHubAuthenticationError("GitHub authentication failed")
        if response.status_code == 404:
            raise GitHubNotFoundError("GitHub resource not found")
        if response.status_code == 422:
            raise GitHubValidationError("GitHub rejected the request")
        retry = cls._retry_after(response)
        reset = cls._reset_epoch(response)
        if response.status_code in {403, 429}:
            try:
                raw = cast(object, response.json())
                parsed = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
                message = str(parsed.get("message", "")).lower()
            except (ValueError, TypeError):
                message = ""
            secondary = response.status_code == 429 or "secondary rate limit" in message
            limited = (
                secondary
                or retry is not None
                or response.headers.get("X-RateLimit-Remaining") == "0"
            )
            if limited:
                raise GitHubRateLimitedError(
                    kind="secondary" if secondary else "primary",
                    retry_after_seconds=retry,
                    reset_epoch=reset,
                )
            raise GitHubPermissionError("GitHub permission denied")
        if response.status_code == 503:
            raise GitHubProviderUnavailableError(retry)
        if response.status_code >= 500:
            raise GitHubProviderUnavailableError()
        raise GitHubResponseError(f"GitHub provider returned HTTP {response.status_code}")
