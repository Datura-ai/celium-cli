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


class LiumHostKeyError(LiumError):
    """A pod presented an SSH host key that differs from the pinned one."""
class RemoteExecutionError(LiumError):
    """A function offloaded with ``@lium.machine`` did not return a result from the pod.

    When the function raised, the caller sees the original exception type and this
    error is its ``__cause__``; ``remote_traceback`` is the traceback from the pod.
    """

    def __init__(
        self,
        message: str,
        *,
        exception_type: str | None = None,
        remote_traceback: str = "",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.exception_type = exception_type
        self.remote_traceback = remote_traceback
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


__all__ = [
    "LiumError",
    "LiumAuthError",
    "LiumRateLimitError",
    "LiumServerError",
    "LiumNotFoundError",
    "LiumPermissionError",
    "LiumHostKeyError",
    "RemoteExecutionError",
]
