"""Exception hierarchy for the Lium SDK."""

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # models imports nothing from here, but keep the import one-way at runtime
    from .models import PodInfo


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


class PodStartError(LiumError):
    """A pod reached a state from which it will never become ready.

    Raised by :meth:`Lium.wait_ready` (and everything built on it) when the pod
    reports a terminal status such as ``FAILED`` or ``STOPPED``, or disappears
    from the account's pod list while being waited for. A timeout is *not* a
    start error: a pod that is merely slow is still returned as ``None``.

    Attributes:
        pod_id: The id that was waited for.
        pod: The last ``PodInfo`` seen for it, or ``None`` if it was never listed.
        status: The last status seen (upper-cased), or ``None`` if never listed.
        history: Every distinct status observed while waiting, in order.
    """

    def __init__(
        self,
        message: str,
        *,
        pod_id: str,
        pod: Optional["PodInfo"] = None,
        status: Optional[str] = None,
        history: Optional[List[str]] = None,
    ):
        super().__init__(message)
        self.pod_id = pod_id
        self.pod = pod
        self.status = status
        self.history = list(history or [])


__all__ = [
    "LiumError",
    "LiumAuthError",
    "LiumRateLimitError",
    "LiumServerError",
    "LiumNotFoundError",
    "LiumPermissionError",
    "LiumHostKeyError",
    "PodStartError",
]
