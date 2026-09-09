"""Templates command implementation."""

import json
from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format
from . import display
from .actions import GetTemplatesAction


@click.command("templates", epilog="Use --format json for machine-readable output (includes template ids).")
@click.argument("search", required=False)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def templates_command(search: Optional[str], output_format: str, json_output: bool):
    """List available Docker templates and images."""
    output_format = resolve_output_format(output_format, json_output)

    # Load data
    lium = Lium()
    ctx = {"lium": lium, "search": search}

    action = GetTemplatesAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading templates", lambda: action.execute(ctx))

    templates = result.data["templates"]

    if output_format == "json":
        click.echo(json.dumps([display.compact_template(t) for t in templates], indent=2, ensure_ascii=False))
        return

    # Check if empty
    if not templates:
        ui.warning("No templates available")
        return

    # Build table
    table, header = display.build_templates_table(templates)

    # Display
    ui.info(header)
    ui.print(table)
