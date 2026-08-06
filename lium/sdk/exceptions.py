"""Exception hierarchy for the Lium SDK."""

class LiumError(Exception):
    """Base exception for Lium SDK."""


class LiumAuthError(LiumError):
    """Authentication error."""


class LiumRateLimitError(LiumError):
    """Rate limit exceeded."""


class LiumServerError(LiumError):
    """Server error."""


class LiumNotFoundError(LiumError):
    """Resource not found (404)."""


class LiumPermissionError(LiumError):
    """The account is not allowed to do this (403)."""


__all__ = [
    "LiumError",
    "LiumAuthError",
    "LiumRateLimitError",
    "LiumServerError",
    "LiumNotFoundError",
    "LiumPermissionError",
]
