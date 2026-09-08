"""`lium workspaces`: the team a key acts in, and — with a browser session — the teams you belong to.

A key is bound to one workspace (lium-platform DAH-2986), so `--workspace` chooses which configured key
a command runs with; the server reports the key's workspace on GET /users/me (DAH-3030) and that field is
also how the CLI knows the server has workspaces at all. Reshaping a team is session-only on the server:
`lium workspaces login` keeps a session token for those subcommands.
"""

import json
import sys
from typing import Optional

import click

from lium.sdk import Lium
from lium.sdk.config import workspace_section
from lium.sdk.models import WorkspaceInfo
from lium.cli import ui
from lium.cli.settings import config as settings
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, ensure_config, handle_errors

from .display import build_members_table, build_workspaces_table, member_json, workspace_json

ROLES = click.Choice(["member", "admin", "owner"])
WORKSPACE_ARG = click.argument("workspace", required=False)


def _client() -> Lium:
    ensure_config()
    return Lium(source="cli")


def _target(lium: Lium, workspace: Optional[str]) -> WorkspaceInfo:
    """The workspace named on the command line, else the configured one, else the key's own."""
    name = workspace or lium.config.workspace
    if name:
        return lium.workspaces.resolve(name)
    return lium.workspaces.require_enabled()


def _member_id(lium: Lium, workspace: WorkspaceInfo, who: str) -> str:
    """A member's user id from an id or an e-mail, looked up in the member list."""
    for member in lium.workspaces.members(workspace.id):
        if who == member.user_id or (member.email and who.lower() == member.email.lower()):
            return member.user_id
    raise CliFailure("member_not_found", f"No member '{who}' in {workspace.name}", EXIT_CONFIGURATION_ERROR)


def _remember(workspace: WorkspaceInfo, api_key: Optional[str] = None) -> None:
    settings.set("workspaces.active", workspace.name)
    settings.set_in_section(workspace_section(workspace.name), "id", workspace.id)
    if api_key:
        settings.set_in_section(workspace_section(workspace.name), "api_key", api_key)


@click.group("workspaces", invoke_without_command=True)
@click.pass_context
def workspaces_command(ctx):
    """Teams: list the workspaces you belong to, pick one, manage members and billing.

    \b
      lium workspaces                      # the workspace this key acts in (all of yours with a session)
      lium workspaces login                # sign in once for create / invite / remove / transfer / delete
      lium workspaces use research         # make 'research' the default for every command
      lium --workspace research ps         # one command in another workspace (its key must be configured)
      lium keys create ci --workspace research --save   # a key for that workspace, saved for --workspace
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(workspaces_list_command)


@workspaces_command.command("list")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def workspaces_list_command(json_output: bool):
    """List workspaces: the one this key acts in, or every one you belong to with a session."""
    lium = _client()
    current = lium.workspaces.require_enabled()
    workspaces = ui.load("Loading workspaces", lium.workspaces.list) if not json_output else lium.workspaces.list()
    if json_output:
        click.echo(json.dumps([workspace_json(w) for w in workspaces], indent=2))
        return
    table, header = build_workspaces_table(workspaces, current.id, lium.config.workspace)
    ui.info(header)
    ui.print(table)
    ui.dim("* = this key acts here" + ("   → = selected with `lium workspaces use`" if lium.config.workspace else ""))
    if not lium.workspaces.session_token:
        ui.dim("A key sees its own workspace only; `lium workspaces login` lists every workspace you belong to")


@workspaces_command.command("members")
@WORKSPACE_ARG
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def workspaces_members_command(workspace: Optional[str], json_output: bool):
    """List the members of a workspace (default: the one this key acts in)."""
    lium = _client()
    target = _target(lium, workspace)
    members = lium.workspaces.members(target.id)
    if json_output:
        click.echo(json.dumps([member_json(m) for m in members], indent=2))
        return
    table, header = build_members_table(target, members)
    ui.info(header)
    ui.print(table)


@workspaces_command.command("use")
@click.argument("workspace")
@click.option("--api-key", "api_key", default=None, help="An API key bound to that workspace, kept for `--workspace`")
@handle_errors
def workspaces_use_command(workspace: str, api_key: Optional[str]):
    """Make WORKSPACE the default for every command; stored in ~/.lium/config.ini.

    A key acts in exactly one workspace, so the default only takes effect for commands that run with a
    key configured for it: pass --api-key here, or `lium keys create <name> --workspace WORKSPACE --save`.
    """
    lium = _client()
    target = lium.workspaces.resolve(workspace)
    current = lium.workspaces.current()
    acts_here = current is not None and current.id == target.id
    _remember(target, api_key or (lium.config.api_key if acts_here else None))
    ui.success(f"Default workspace: {target.name} ({target.role})")
    if not api_key and not acts_here:
        ui.warning(
            f"No API key is configured for {target.name}; the current key acts in "
            f"{current.name if current else 'another workspace'}. "
            f"Run `lium keys create <name> --workspace {target.name} --save`."
        )


@workspaces_command.command("login")
@click.option("--email", prompt=True, help="The e-mail of your Lium account")
@click.option(
    "--password-stdin", is_flag=True, help="Read the password from stdin (for scripts) instead of prompting"
)
@handle_errors
def workspaces_login_command(email: str, password_stdin: bool):
    """Sign in with e-mail and password; the session token is kept for the session-only subcommands.

    Accounts created with GitHub or Google sign-in set a password with "Forgot password" on lium.io first.
    """
    password = sys.stdin.readline().rstrip("\n") if password_stdin else click.prompt("Password", hide_input=True)
    lium = _client()
    token = lium.workspaces.login(email, password)
    settings.set("session.token", token)
    ui.success("Signed in; `lium workspaces` subcommands can now manage your teams")


@workspaces_command.command("create")
@click.argument("name")
@click.option("--use", "make_default", is_flag=True, help="Also make it the default workspace")
@handle_errors
def workspaces_create_command(name: str, make_default: bool):
    """Create a workspace; you become its owner and billing owner."""
    lium = _client()
    lium.workspaces.require_enabled()
    workspace = lium.workspaces.create(name)
    ui.success(f"Created {workspace.name} ({workspace.id})")
    if make_default:
        _remember(workspace)
    ui.dim(f"Next: lium keys create <name> --workspace {workspace.name} --save")


@workspaces_command.command("invite")
@click.argument("email")
@WORKSPACE_ARG
@click.option("--role", type=ROLES, default="member", show_default=True)
@handle_errors
def workspaces_invite_command(email: str, workspace: Optional[str], role: str):
    """E-mail an invitation to join a workspace; the address need not have a Lium account yet."""
    lium = _client()
    target = _target(lium, workspace)
    invitation = lium.workspaces.invite(target.id, email, role)
    if invitation.get("email_sent") is False:
        ui.warning(f"Invitation recorded but the e-mail to {email} did not leave; revoke it on lium.io and retry")
    else:
        ui.success(f"Invited {email} to {target.name} as {role}; the link expires {invitation.get('expires_at', '')}")


@workspaces_command.command("remove")
@click.argument("who", metavar="USER_ID_OR_EMAIL")
@WORKSPACE_ARG
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@handle_errors
def workspaces_remove_command(who: str, workspace: Optional[str], yes: bool):
    """Remove a member from a workspace (an owner or the billing owner cannot be removed — the server says so)."""
    lium = _client()
    target = _target(lium, workspace)
    user_id = _member_id(lium, target, who)
    if not yes and not ui.confirm(f"Remove {who} from {target.name}?"):
        ui.warning("Nothing removed")
        return
    ui.success(lium.workspaces.remove_member(target.id, user_id).get("message", "Member removed"))


@workspaces_command.command("transfer-billing")
@click.argument("who", metavar="USER_ID_OR_EMAIL")
@WORKSPACE_ARG
@handle_errors
def workspaces_transfer_billing_command(who: str, workspace: Optional[str]):
    """Hand the bill to another member: at once for an owner or admin, after their acceptance for a member."""
    lium = _client()
    target = _target(lium, workspace)
    user_id = _member_id(lium, target, who)
    after = lium.workspaces.transfer_billing(target.id, user_id)
    if after.pending_billing_owner_user_id:
        ui.success(f"Transfer requested; {who} pays for {target.name} once they accept on lium.io")
    else:
        ui.success(f"{who} now pays for {target.name}")


@workspaces_command.command("delete")
@WORKSPACE_ARG
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@handle_errors
def workspaces_delete_command(workspace: Optional[str], yes: bool):
    """Delete a workspace (owners only; refused while it still has running pods or volumes)."""
    lium = _client()
    target = _target(lium, workspace)
    if not yes and not ui.confirm(f"Delete workspace {target.name} ({target.id})?"):
        ui.warning("Nothing deleted")
        return
    ui.success(lium.workspaces.delete(target.id).get("message", "Workspace deleted"))
    if settings.get("workspaces.active") == target.name:
        settings.unset("workspaces.active")


__all__ = ["workspaces_command"]
