"""Balance command."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format


@click.command("balance", epilog="Use --format json (or --json) for machine-readable output.")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, help="Alias for --format json")
@handle_errors
def balance_command(output_format: str, json_output: bool):
    """Show the current Lium account balance.

    \b
    Examples:
      lium balance
      lium balance --format json
    """
    output_format = resolve_output_format(output_format, json_output)
    balance = Lium().balance()

    if output_format == "json":
        # ``balance_usd`` is kept for existing consumers; ``balance`` + ``currency``
        # is the shape shared with the other commands.
        click.echo(json.dumps(
            {"balance": balance, "balance_usd": balance, "currency": "USD"}, sort_keys=True
        ))
        return

    ui.info(f"Current balance: {balance} USD")
