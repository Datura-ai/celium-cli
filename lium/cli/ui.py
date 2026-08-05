"""UI library - thin wrapper around Rich for user interaction.

This module provides a clean interface to Rich, handling only I/O operations.
All formatting and domain logic should live in command-specific modules.
"""

import os
from typing import Callable, TypeVar, List, Tuple, Any
from contextlib import contextmanager
from rich.prompt import Confirm, Prompt
from rich.table import Table

from lium.cli.utils import console, loading_status, notice_console

T = TypeVar("T")


def is_debug() -> bool:
    """Check if debug mode is enabled via LIUM_DEBUG env var."""
    return os.environ.get("LIUM_DEBUG", "").strip() in ("1", "true", "True", "TRUE")


# ============================================================================
# Loading & Status
# ============================================================================

@contextmanager
def loading(message: str):
    """Context manager for loading status.

    Example:
        with ui.loading("Loading pods"):
            pods = lium.ps()
    """
    with loading_status(message, ""):
        yield


def load(message: str, fn: Callable[[], T]) -> T:
    """Execute a function with loading status.

    Args:
        message: Loading message to display
        fn: Function to execute

    Returns:
        Result of the function

    Example:
        pods = ui.load("Loading pods", lambda: lium.ps())
    """
    with loading_status(message, ""):
        return fn()


# ============================================================================
# User Input
# ============================================================================

def confirm(message: str, default: bool = False) -> bool:
    """Ask user for yes/no confirmation.

    Args:
        message: Question to ask
        default: Default answer if user just presses enter

    Returns:
        True if confirmed, False otherwise
    """
    return Confirm.ask(message, default=default)


def prompt(message: str, default: str = "") -> str:
    """Prompt user for text input.

    Args:
        message: Prompt message
        default: Default value if user just presses enter

    Returns:
        User's input
    """
    return Prompt.ask(message, default=default)


# ============================================================================
# Messages
# ============================================================================

def success(message: str) -> None:
    """Display success message (green)."""
    console.success(message)


def error(message: str) -> None:
    """Display error message (red)."""
    console.error(message)


def warning(message: str) -> None:
    """Display warning message (yellow)."""
    console.warning(message)


def info(message: str) -> None:
    """Display info message (cyan/blue)."""
    console.info(message)


def notice(message: str) -> None:
    """Display an informational startup notice on stderr."""
    notice_console().info(message)


def notice_warning(message: str) -> None:
    """Display a warning startup notice on stderr."""
    notice_console().warning(message)


def notice_debug(message: str) -> None:
    """Display a debug startup notice on stderr, only if LIUM_DEBUG=1."""
    if is_debug():
        notice_console().dim(f"[DEBUG] {message}")


def dim(message: str) -> None:
    """Display dimmed/secondary text."""
    console.dim(message)


def print(*args, **kwargs) -> None:
    """Display plain message."""
    console.print(*args, **kwargs)


def debug(message: str) -> None:
    """Display debug message only if LIUM_DEBUG=1."""
    if is_debug():
        console.dim(f"[DEBUG] {message}")


# ============================================================================
# Tables
# ============================================================================

def table(headers: List[str], rows: List[List[str]], **kwargs) -> None:
    """Display a table.

    Args:
        headers: Column headers
        rows: List of rows (each row is a list of values)
        **kwargs: Additional arguments passed to Rich Table

    Example:
        ui.table(
            ["Name", "Status", "Price"],
            [
                ["pod-1", "running", "$0.50/h"],
                ["pod-2", "stopped", "$0.30/h"],
            ]
        )
    """
    rich_table = Table(**kwargs)

    for header in headers:
        rich_table.add_column(header)

    for row in rows:
        rich_table.add_row(*row)

    console.print(rich_table)


# ============================================================================
# Styling Helpers
# ============================================================================

def styled(text: str, style: str) -> str:
    """Get styled text using console theme.

    Args:
        text: Text to style
        style: Style key from theme (success, error, warning, info, dim, etc.)

    Returns:
        Styled text string
    """
    return console.get_styled(text, style)
