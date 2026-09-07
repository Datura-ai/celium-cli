"""Utility helpers for the Lium SDK."""

import hashlib
import random
import re
import time
from functools import wraps
from typing import Callable, TypeVar

import requests

from .exceptions import LiumRateLimitError, LiumServerError

F = TypeVar("F", bound=Callable[..., object])

# Human-friendly ID parts
ADJECTIVES = ["swift", "brave", "calm", "eager", "gentle", "cosmic", "golden", "lunar", "zesty", "noble"]
NOUNS = ["hawk", "lion", "eagle", "fox", "wolf", "shark", "raven", "matrix", "comet", "orbit"]


def generate_huid(id_str: str) -> str:
    """Generate human-readable ID from UUID."""
    if not id_str:
        return "invalid"

    digest = hashlib.md5(id_str.encode()).hexdigest()
    adj = ADJECTIVES[int(digest[:4], 16) % len(ADJECTIVES)]
    noun = NOUNS[int(digest[4:8], 16) % len(NOUNS)]
    return f"{adj}-{noun}-{digest[-2:]}"


# Short names users type that are not literally the extracted type.
GPU_TYPE_ALIASES = {
    "PRO6000": "RTXPRO6000",
    "RTX6000PRO": "RTXPRO6000",
    "6000PRO": "RTXPRO6000",
}


def extract_gpu_type(machine_name: str) -> str:
    """Extract GPU type from machine name.

    Examples:
        "NVIDIA H100 80GB HBM3"                            -> "H100"
        "NVIDIA GeForce RTX 4090"                          -> "RTX4090"
        "NVIDIA RTX 6000 Ada Generation"                   -> "RTX6000"
        "NVIDIA RTX PRO 6000 Blackwell Server Edition"     -> "RTXPRO6000"
        "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"-> "RTXPRO6000"
        "NVIDIA A100-SXM4-80GB"                            -> "A100"
    """
    patterns = [
        # "RTX PRO 6000 Blackwell ..." must be tried before the plain RTX pattern, otherwise the
        # word "PRO" breaks the match and the type falls through to the last word ("Edition").
        (r"RTX\s*PRO\s*(\d{4})", lambda m: f"RTXPRO{m.group(1)}"),
        (r"RTX\s*(\d{4})", lambda m: f"RTX{m.group(1)}"),
        (r"([HBL])(\d{2,3}S?)", lambda m: f"{m.group(1)}{m.group(2)}"),
        (r"A(\d{2,4})", lambda m: f"A{m.group(1)}"),
    ]
    for pattern, fmt in patterns:
        if match := re.search(pattern, machine_name, re.I):
            return fmt(match).upper()
    return machine_name.split()[-1] if machine_name else "Unknown"


def normalize_gpu_short(gpu_short: str) -> str:
    """Canonical form of a user-typed GPU short name for comparisons.

    Upper-cases, drops spaces/hyphens/underscores and applies :data:`GPU_TYPE_ALIASES`,
    so ``"rtx pro 6000"``, ``"RTX-PRO-6000"``, ``"pro6000"`` all become ``"RTXPRO6000"``.
    """
    key = re.sub(r"[\s_\-]+", "", gpu_short or "").upper()
    return GPU_TYPE_ALIASES.get(key, key)


def expand_gpu_shorthand(gpu_short: str) -> str:
    """Expand GPU shorthand to a pattern that matches full machine names.

    Examples:
        A100 -> "A100" (matches "NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-40GB", etc.)
        H200 -> "H200" (matches "NVIDIA H200", etc.)
        RTX4090 -> "RTX 4090" (matches "NVIDIA GeForce RTX 4090", etc.)

    Args:
        gpu_short: Short GPU name like "A100", "H200", "RTX4090"

    Returns:
        Pattern string that can be used to filter machine names.
    """
    # Already a full name or pattern, return as-is
    if len(gpu_short) > 10 or " " in gpu_short:
        return gpu_short

    gpu_upper = gpu_short.upper()

    # Handle RTX cards - need to add space between RTX and number
    if gpu_upper.startswith("RTX"):
        # RTX4090 -> RTX 4090
        match = re.match(r"RTX(\d+)", gpu_upper)
        if match:
            return f"RTX {match.group(1)}"

    # For A-series (A100, A6000, etc.) and H-series (H100, H200, etc.)
    # Just return as-is since the API does substring matching
    # "A100" will match "NVIDIA A100-SXM4-80GB"
    return gpu_short


TRANSIENT_ERRORS = (LiumRateLimitError, LiumServerError, requests.RequestException)


def with_retry(max_attempts: int = 3, delay: float = 1.0, exceptions: tuple = TRANSIENT_ERRORS):
    """Retry decorator for API calls: back off and repeat on ``exceptions``."""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(delay * (2 ** attempt) + random.uniform(0, 0.5))
        return wrapper  # type: ignore[misc]
    return decorator


__all__ = ["generate_huid", "extract_gpu_type", "expand_gpu_shorthand", "normalize_gpu_short", "GPU_TYPE_ALIASES", "with_retry"]
