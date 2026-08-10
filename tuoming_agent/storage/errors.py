class StorageError(RuntimeError):
    """Base storage error."""


class RecordNotFoundError(StorageError):
    """Raised when a scoped record does not exist."""


class AuthorizationError(StorageError):
    """Raised when a tenant tries to access another tenant's record."""
