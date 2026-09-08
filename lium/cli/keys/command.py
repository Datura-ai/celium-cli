"""`lium keys`: API keys of a workspace (session-only on the server: a key cannot mint keys)."""

import json
from typing import Optional

import click
from rich.table import Table

from lium.sdk import Lium
from lium.sdk.config import workspace_section
from lium.cli import ui
from lium.cli.settings import config as settings
from lium.cli.utils import ensure_config, format_date, handle_errors


def _client() -> Lium:
    ensure_config()
    return Lium(source="cli")


def _target(lium: Lium, workspace: Optional[str]):
    name = workspace or lium.config.workspace
    return lium.workspaces.resolve(name) if name else lium.workspaces.require_enabled()


@click.group("keys", invoke_without_command=True)
@click.pass_context
def keys_command(ctx):
    """API keys, per workspace. Needs `lium workspaces login` first (keys cannot manage keys)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(keys_list_command)


@keys_command.command("list")
@click.option("--workspace", "-w", default=None, help="Workspace name or id (default: the current one)")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def keys_list_command(workspace: Optional[str], json_output: bool):
    """List the API keys of a workspace (owners and admins)."""
    lium = _client()
    target = _target(lium, workspace)
    keys = lium.workspaces.list_keys(target.id)
    if json_output:
        click.echo(json.dumps(keys, indent=2))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Name", "Scopes", "Created", "Last used", "ID"):
        table.add_column(column, overflow="fold")
    for key in keys:
        table.add_row(
            key.get("name", ""),
            ",".join(key.get("scopes") or []) or "—",
            format_date(key["created_at"]) if key.get("created_at") else "—",
            format_date(key["last_used"]) if key.get("last_used") else "—",
            key.get("id", ""),
        )
    ui.info(f"API keys of {target.name}  ({len(keys)} total)")
    ui.print(table)


@keys_command.command("create")
@click.argument("name")
@click.option("--workspace", "-w", default=None, help="Workspace name or id the key is bound to (default: current)")
@click.option("--save", is_flag=True, help="Keep the key in ~/.lium/config.ini for `--workspace <name>`")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def keys_create_command(name: str, workspace: Optional[str], save: bool, json_output: bool):
    """Create an API key bound to a workspace; the key is printed once."""
    lium = _client()
    target = _target(lium, workspace)
    key = lium.workspaces.create_key(name, target.id)
    if save:
        settings.set_in_section(workspace_section(target.name), "id", target.id)
        settings.set_in_section(workspace_section(target.name), "api_key", key.get("key", ""))
    if json_output:
        click.echo(json.dumps({**key, "workspace_name": target.name}, indent=2))
        return
    ui.success(f"Key '{name}' created in {target.name}")
    ui.print(key.get("key", ""))
    ui.dim(
        f"Saved for `lium --workspace {target.name} …`" if save else f"Not saved; add --save to use it with --workspace"
    )
