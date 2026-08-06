"""Config unset command implementation."""

import click

from lium.cli.utils import CliFailure, EXIT_GENERAL_ERROR, handle_errors
from .actions import UnsetConfigAction


@click.command(name="unset")
@click.argument("key")
@handle_errors
def config_unset_command(key: str):
    """Remove a configuration value."""

    # Execute
    ctx = {"key": key}

    action = UnsetConfigAction()
    result = action.execute(ctx)

    if not result.ok:
        raise CliFailure("key_not_found", result.error, EXIT_GENERAL_ERROR)
