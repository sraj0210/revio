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


class PersistenceError(RevioError):
    """A persistence operation failed safely."""


class PersistenceUnavailableError(PersistenceError):
    """Persistence could not complete within its bounded availability budget."""


class PersistenceNotCommittedError(PersistenceUnavailableError):
    """A durable operation was confirmed not to have committed."""


class PersistenceIndeterminateError(PersistenceError):
    """A durable operation's commit disposition could not be established safely."""


class PersistenceIntegrityError(PersistenceError):
    """Persistence reached a durable state that cannot be accepted safely."""


class MigrationRequiredError(PersistenceError):
    """The database schema is not at the required revision."""


class QueueCapacityError(PersistenceError):
    """The active durable queue is at its configured capacity."""


class DeliveryIntegrityError(PersistenceError):
    """A delivery identity was reused with different content."""


class InvalidJobError(RevioError):
    """A durable job cannot be decoded or processed safely."""


class LeaseLostError(RevioError):
    """The worker no longer owns the lease required for a state transition."""


class RetentionIntegrityError(PersistenceError):
    """Retention found a conflicting permanent delivery identity."""


class ProviderTransientError(RevioError):
    """A provider read may be retried safely."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderCallAmbiguousError(RevioError):
    """A provider call may have been accepted without a trustworthy response."""


class ProviderCallRejectedError(RevioError):
    """A provider explicitly rejected a call before producing a result."""


class ProviderCallSafeRetryError(ProviderTransientError):
    """A Messages request was explicitly rejected without generation and may retry."""


class ProviderCallTerminalError(RevioError):
    """A provider outcome is terminal for generation but maps to a safe partial artifact."""


class ProviderCallObservedTerminalError(RevioError):
    """A trustworthy Messages response was observed but cannot produce a usable review."""

    def __init__(self, message: str, *, usage: object, reason: str) -> None:
        super().__init__(message)
        self.usage = usage
        self.reason = reason


class ProviderCallObservedInvalidResponseError(RevioError):
    """A 2xx Messages response was observed but cannot be normalized safely."""


class MalformedProviderOutputError(RevioError):
    """A trustworthy provider response failed local semantic validation."""

    def __init__(self, message: str, *, usage: object) -> None:
        super().__init__(message)
        self.usage = usage


class ReconciliationIntegrityError(RevioError):
    """Provider reconciliation returned contradictory exact identities."""


class ProviderWriteAmbiguousError(RevioError):
    """A provider write may have succeeded without a trustworthy response."""


class ProviderWriteRejectedError(RevioError):
    """A provider proved that a write did not create an object."""


class ProviderAnchorRejectedError(ProviderWriteRejectedError):
    """A review POST was positively rejected for invalid inline anchors."""
