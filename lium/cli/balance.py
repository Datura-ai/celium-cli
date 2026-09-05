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

    \b
    Examples:
      lium balance
      lium balance --json
    """
    lium = Lium()
    balance = lium.balance()

    # Name the key: a balance that does not match `up`'s "insufficient balance"
    # usually means the two commands resolved different keys.
    key_config = getattr(lium, "config", None)

    if json_output:
        payload = {"balance_usd": balance}
        if key_config is not None:
            payload["api_key_fingerprint"] = key_config.api_key_fingerprint
            payload["api_key_source"] = key_config.api_key_source
        click.echo(json.dumps(payload, sort_keys=True))
        return

    ui.info(f"Current balance: {balance} USD")
    if key_config is not None:
        ui.dim(f"Account: {key_config.api_key_description}")
