"""Custom exception hierarchy for personal-finance.

Define all project-specific exceptions here. Catch at the boundary layer
(HTTP handlers, CLI entrypoints) — not deep in business logic.
"""


class PersonalfinanceError(Exception):
    """Base exception for all personal-finance errors."""


class ConfigurationError(PersonalfinanceError):
    """Raised when application configuration is invalid."""


class NotFoundError(PersonalfinanceError):
    """Raised when a requested resource does not exist."""


class ValidationError(PersonalfinanceError):
    """Raised when input data fails validation."""


class ExternalServiceError(PersonalfinanceError):
    """Raised when an external service call fails."""
