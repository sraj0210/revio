"""Safe GitHub error classification."""

from revio.errors import (
    ProviderTransientError,
    ProviderWriteAmbiguousError,
    ProviderWriteRejectedError,
    RevioError,
)


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


class GitHubAmbiguousNotFoundError(GitHubNotFoundError):
    pass


class GitHubInvalidRefError(GitHubError):
    pass


class GitHubUnsupportedObjectError(GitHubError):
    pass


class GitHubTransportError(GitHubError, ProviderTransientError):
    pass


class GitHubResponseError(GitHubError):
    pass


class GitHubValidationError(GitHubError, ProviderWriteRejectedError):
    pass


class GitHubAmbiguousWriteError(GitHubError, ProviderWriteAmbiguousError):
    """A GitHub write may have succeeded but no trustworthy response was observed."""


class GitHubProviderUnavailableError(GitHubError, ProviderTransientError):
    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("GitHub provider is temporarily unavailable")
        self.retry_after_seconds = retry_after_seconds


class GitHubRateLimitedError(GitHubError, ProviderTransientError):
    def __init__(
        self,
        *,
        kind: str,
        retry_after_seconds: float | None = None,
        reset_epoch: int | None = None,
    ) -> None:
        super().__init__("GitHub rate limit exceeded")
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.reset_epoch = reset_epoch
