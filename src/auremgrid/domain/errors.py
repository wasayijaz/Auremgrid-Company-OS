class AuremgridError(Exception):
    """Base error for the Company OS."""


class AuthorizationError(AuremgridError):
    """Raised when an actor cannot perform the requested operation."""


class AuthenticationError(AuremgridError):
    """Raised when a request does not carry a valid active credential."""


class NotFoundError(AuremgridError):
    """Raised when a required workspace, actor, or record is missing."""


class ValidationError(AuremgridError):
    """Raised when a request is malformed."""
