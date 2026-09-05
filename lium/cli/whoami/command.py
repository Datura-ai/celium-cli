"""`lium whoami`: which key, which account, which machine."""

import json

import click
from rich.markup import escape

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, handle_errors
from .identity import Identity, collect_identity


def _yes_no(value):
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def render(identity: Identity) -> None:
    rows = [
        ("API key", f"{identity.api_key_fingerprint}  ({identity.api_key_source or 'not configured'})"),
        ("Account", identity.account_id or "-"),
    ]
    if identity.email:
        rows.append(("Email", identity.email))
    rows += [
        ("Balance", f"${identity.balance_usd:.2f}" if identity.balance_usd is not None else "-"),
        ("API", (
            f"{identity.api_base_url}  reachable, {identity.api_latency_ms} ms"
            if identity.api_reachable
            else f"{identity.api_base_url or ''}  unreachable: {identity.api_error}".strip()
        )),
        ("SSH key", (
            f"{identity.ssh_key_path}  registered: {_yes_no(identity.ssh_key_registered)}"
            if identity.ssh_key_path else "none found in ~/.ssh"
        )),
        ("CLI", f"lium {identity.cli_version}"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        ui.print(f"[dim]{label.ljust(width)}[/dim]  {escape(value)}")
    for warning in identity.warnings:
        ui.warning(warning)


@click.command("whoami")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def whoami_command(json_output: bool):
    """Show the API key in use (fingerprint and source), the account, balance, SSH key and CLI version.

    \b
    Examples:
      lium whoami
      lium whoami --json | jq .account_id
    """
    identity = ui.load("Checking", collect_identity) if not json_output else collect_identity()

    if json_output:
        click.echo(json.dumps(identity.to_dict(), sort_keys=True))
    else:
        render(identity)

    if not identity.has_api_key:
        raise CliFailure("api_key_missing", "No API key configured", EXIT_CONFIGURATION_ERROR)
    if identity.api_reachable is False:
        raise CliFailure("api_unreachable", identity.api_error or "API unreachable", EXIT_API_ERROR)
