"""Typed errors shared across application and adapter boundaries."""


class RevioError(Exception):
    """Base class for expected Revio failures."""


class IncompleteReviewInputError(RevioError):
    """A review cannot safely proceed with incomplete provider input."""


class RegistryError(RevioError):
    """Base registry failure."""


class DuplicateRegistrationError(RegistryError):
    """Raised when an identifier is registered more than once."""


class UnknownProviderError(RegistryError):
    """Raised when an unregistered provider is requested."""


class UnknownModelAliasError(RegistryError):
    """Raised when an administrator-approved model alias is unknown."""
