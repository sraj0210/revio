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
