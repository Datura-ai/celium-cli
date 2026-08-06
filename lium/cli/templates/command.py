"""Templates command implementation."""

from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors
from . import display
from .actions import GetTemplatesAction


@click.command("templates")
@click.argument("search", required=False)
@handle_errors
def templates_command(search: Optional[str]):
    """List available Docker templates and images."""
    # Load data
    lium = Lium()
    ctx = {"lium": lium, "search": search}

    action = GetTemplatesAction()
    result = ui.load("Loading templates", lambda: action.execute(ctx))

    templates = result.data["templates"]

    # Check if empty
    if not templates:
        ui.warning("No templates available")
        return

    # Build table
    table, header = display.build_templates_table(templates)

    # Display
    ui.info(header)
    ui.print(table)
