"""Balance command."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors


@click.command("balance")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def balance_command(json_output: bool):
    """Show the current Lium account balance.

    Running pods draw on it per second at their $/h price; the platform
    debits the balance every 5 minutes.

    \b
    Examples:
      lium balance
      lium balance --json
    """
    balance = Lium().balance()

    if json_output:
        click.echo(json.dumps({"balance_usd": balance}, sort_keys=True))
        return

    ui.info(f"Current balance: {balance} USD")
