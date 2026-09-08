"""Workspaces display formatting."""

from typing import List, Optional, Tuple

from rich.table import Table
from rich.text import Text

from lium.sdk.models import WorkspaceInfo, WorkspaceMember
from lium.cli.utils import format_date


def _table() -> Table:
    return Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))


def build_workspaces_table(
    workspaces: List[WorkspaceInfo], current_id: Optional[str], active_name: Optional[str]
) -> Tuple[Table, str]:
    header = f"{Text('Workspaces', style='bold')}  ({len(workspaces)} total)"
    table = _table()
    table.add_column("", justify="left", width=2, no_wrap=True)
    table.add_column("Name", justify="left", ratio=3, min_width=15, overflow="ellipsis")
    table.add_column("Role", justify="left", width=8, no_wrap=True)
    table.add_column("Billing owner", justify="left", ratio=3, min_width=20, overflow="fold")
    table.add_column("ID", justify="left", ratio=3, min_width=20, overflow="fold")
    for workspace in workspaces:
        marks = ("*" if workspace.id == current_id else "") + (
            "→" if active_name and active_name.lower() == workspace.name.lower() else ""
        )
        table.add_row(marks, workspace.name, workspace.role, workspace.billing_owner_user_id, workspace.id)
    return table, header


def build_members_table(workspace: WorkspaceInfo, members: List[WorkspaceMember]) -> Tuple[Table, str]:
    header = f"{Text(workspace.name, style='bold')}  ({len(members)} member{'s' if len(members) != 1 else ''})"
    table = _table()
    table.add_column("Name", justify="left", ratio=2, min_width=12, overflow="ellipsis")
    table.add_column("E-mail", justify="left", ratio=3, min_width=18, overflow="fold")
    table.add_column("Role", justify="left", width=8, no_wrap=True)
    table.add_column("Pays", justify="left", width=5, no_wrap=True)
    table.add_column("Joined", justify="right", width=12, no_wrap=True)
    table.add_column("User ID", justify="left", ratio=3, min_width=20, overflow="fold")
    for member in members:
        table.add_row(
            member.name or "—",
            member.email or "—",
            member.role,
            "yes" if member.is_billing_owner else "",
            format_date(member.joined_at) if member.joined_at else "—",
            member.user_id,
        )
    return table, header


def workspace_json(workspace: WorkspaceInfo) -> dict:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "role": workspace.role,
        "billing_owner_user_id": workspace.billing_owner_user_id,
        "pending_billing_owner_user_id": workspace.pending_billing_owner_user_id,
        "is_personal": workspace.is_personal,
        "created_at": workspace.created_at,
    }


def member_json(member: WorkspaceMember) -> dict:
    return {
        "user_id": member.user_id,
        "name": member.name,
        "email": member.email,
        "role": member.role,
        "is_billing_owner": member.is_billing_owner,
        "joined_at": member.joined_at,
    }
