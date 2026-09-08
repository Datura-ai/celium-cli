"""The workspace a command acts in, as one line under a table (lium-platform DAH-3030)."""

from typing import Optional

from lium.sdk.models import WorkspaceInfo


def current_workspace(lium) -> Optional[WorkspaceInfo]:
    """The workspace this client's key acts in; None on a server without workspaces or on any failure.

    Decoration only: a command must never fail because the context line could not be read, and
    test doubles of ``Lium`` may not carry ``workspaces`` at all.
    """
    try:
        return lium.workspaces.current()
    except Exception:
        return None


def context_line(lium) -> Optional[str]:
    """``Workspace: Research (member) · personal`` — or None when the server has no workspaces.

    When ``--workspace`` / ``lium workspaces use`` asked for a workspace the key does not act in, the
    line says so, because a key selects its workspace and no flag can move it.
    """
    workspace = current_workspace(lium)
    if workspace is None:
        return None
    requested = getattr(getattr(lium, "config", None), "workspace", None)
    line = f"Workspace: {workspace.name} ({workspace.role})"
    if workspace.is_personal:
        line += " · personal"
    if requested and requested not in (workspace.id, workspace.name) and requested.lower() != workspace.name.lower():
        line += (
            f" — the configured key acts here, not in '{requested}'; "
            f"run `lium keys create --workspace {requested} --save` to get one that does"
        )
    return line
