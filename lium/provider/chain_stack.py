"""Why the chain stack may be missing, in words a caller can act on.

The chain stack is optional (DAH-2553): renting never needs it, and its build
fails on Python 3.14. A caller that hits this needs to know which of the two
situations it is in, because the fix is different — and, when the fix is
"install the extra", the command that does it depends on how ``lium`` itself
was installed (DAH-2943: a ``uv tool`` / ``mine.sh`` install told to
``pip install`` puts the extra into a Python the CLI does not run from).
"""

import os
import sys

LAST_SUPPORTED_PYTHON = (3, 13)

# `uv tool install` and pipx each leave a marker at the root of the venv they
# manage; the standalone binary runs frozen. Anything else is a plain pip/venv.
_UV_TOOL_MARKER = "uv-receipt.toml"
_PIPX_MARKER = "pipx_metadata.json"


def install_command_for_this_interpreter() -> str:
    """The command that adds the ``provider`` extra to *this* ``lium``."""
    if getattr(sys, "frozen", False):
        return "reinstall the standalone binary: curl -fsSL https://lium.io/install.sh | bash"
    if os.path.exists(os.path.join(sys.prefix, _UV_TOOL_MARKER)):
        return 'uv tool install --force "lium.io[provider]"'
    if os.path.exists(os.path.join(sys.prefix, _PIPX_MARKER)):
        return 'pipx install --force "lium.io[provider]"'
    return 'pip install "lium.io[provider]"'


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
    return f"the chain stack is not installed: {install_command_for_this_interpreter()}"
