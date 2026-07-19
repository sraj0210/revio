"""Safe GitHub error classification."""

from dataclasses import dataclass

from revio.errors import RevioError


class GitHubError(RevioError):
    pass


class GitHubConfigurationError(GitHubError):
    pass


class GitHubAuthenticationError(GitHubError):
    pass


class GitHubPermissionError(GitHubError):
    pass


class GitHubNotFoundError(GitHubError):
    pass


class GitHubUnsupportedObjectError(GitHubError):
    pass


class GitHubTransportError(GitHubError):
    pass


class GitHubResponseError(GitHubError):
    pass


@dataclass(frozen=True)
class GitHubRateLimitedError(GitHubError):
    retry_after_seconds: float | None = None
