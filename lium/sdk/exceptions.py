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


class LiumInsufficientBalanceError(LiumPermissionError):
    """The account cannot pay for this (403 with a balance reason).

    ``required`` and ``available`` are USD amounts when the server said what
    they are, else ``None``. Callers that catch :class:`LiumPermissionError`
    keep working; ones that want the numbers catch this class.
    """

    def __init__(
        self,
        message: str,
        *,
        required: float | None = None,
        available: float | None = None,
    ) -> None:
        super().__init__(message)
        self.required = required
        self.available = available


__all__ = [
    "LiumError",
    "LiumAuthError",
    "LiumRateLimitError",
    "LiumServerError",
    "LiumNotFoundError",
    "LiumPermissionError",
    "LiumInsufficientBalanceError",
]
