"""Why the chain stack may be missing, in words a caller can act on.

The chain stack is optional (DAH-2553): renting never needs it, and its build
fails on Python 3.14. A caller that hits this needs to know which of the two
situations it is in, because the fix is different.
"""

import sys

LAST_SUPPORTED_PYTHON = (3, 13)


def missing_chain_stack_message() -> str:
    """Explain the absence and name the fix for this interpreter."""
    running_python = sys.version_info[:2]
    if running_python > LAST_SUPPORTED_PYTHON:
        running = f"{running_python[0]}.{running_python[1]}"
        return (
            f"the chain stack does not build on Python {running} (bittensor-drand "
            f"needs a Rust toolchain and fails there). Use Python "
            f"{LAST_SUPPORTED_PYTHON[0]}.{LAST_SUPPORTED_PYTHON[1]} or the standalone "
            f"binary from https://lium.io/install.sh, which ships it prebuilt"
        )
    return 'the chain stack is not installed: pip install "lium.io[provider]"'
