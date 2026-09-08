"""Workspaces: teams with roles and a billing owner (lium-platform DAH-2975 / DAH-2986 / DAH-3030 / DAH-3031).

An API key acts in exactly one workspace and the server tells which on ``GET /users/me`` — that field
is also how this client knows the server has workspaces at all. Reads of the workspace the key acts
in work with the key; anything that reshapes a team (create, invite, remove, transfer billing,
delete) and anything on ``/keys`` is session-only on the server, so those calls need a browser-session
token (``Lium.workspaces.login`` or LIUM_SESSION_TOKEN) and raise :class:`LiumAuthError` without one.
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .exceptions import LiumAuthError, LiumError
from .models import WorkspaceInfo, WorkspaceMember

if TYPE_CHECKING:  # pragma: no cover
    from .client import Lium

WORKSPACE_HEADER = "X-Lium-Workspace-Id"
NOT_ENABLED = "Workspaces are not enabled on this server"
NEEDS_SESSION = (
    "This needs a browser session, not an API key: run `lium workspaces login` "
    "(or set LIUM_SESSION_TOKEN)"
)


def _info(d: Dict[str, Any]) -> WorkspaceInfo:
    return WorkspaceInfo(
        id=d.get("id", ""),
        name=d.get("name", ""),
        role=d.get("role", ""),
        billing_owner_user_id=d.get("billing_owner_user_id", ""),
        pending_billing_owner_user_id=d.get("pending_billing_owner_user_id"),
        created_at=d.get("created_at"),
        is_personal=d.get("is_personal"),
    )


def _member(d: Dict[str, Any]) -> WorkspaceMember:
    return WorkspaceMember(
        user_id=d.get("user_id", ""),
        name=d.get("name", ""),
        email=d.get("email"),
        role=d.get("role", ""),
        is_billing_owner=bool(d.get("is_billing_owner")),
        joined_at=d.get("joined_at"),
    )


class WorkspacesClient:
    def __init__(self, lium: "Lium"):
        self._lium = lium
        self._me: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ capability
    def _me_payload(self) -> Dict[str, Any]:
        if self._me is None:
            self._me = self._lium._request("GET", "/users/me").json()
        return self._me

    def enabled(self) -> bool:
        """Whether the server has workspaces switched on: ``GET /users/me`` carries ``workspace``."""
        return isinstance(self._me_payload().get("workspace"), dict)

    def current(self) -> Optional[WorkspaceInfo]:
        """The workspace this client's API key acts in, or None on a server without workspaces."""
        workspace = self._me_payload().get("workspace")
        return _info(workspace) if isinstance(workspace, dict) else None

    def require_enabled(self) -> WorkspaceInfo:
        current = self.current()
        if current is None:
            raise LiumError(NOT_ENABLED)
        return current

    # ------------------------------------------------------------------ session
    @property
    def session_token(self) -> Optional[str]:
        return self._lium.config.session_token

    def login(self, email: str, password: str) -> str:
        """Sign in with e-mail and password (``POST /users/login``) and keep the token on this client."""
        response = self._lium._request(
            "POST", "/users/login", headers=self._plain_headers(), json={"email": email, "password": password}
        )
        token = response.json().get("token")
        if not token:
            raise LiumAuthError("Login did not return a session token")
        self._lium.config.session_token = token
        return token

    def _plain_headers(self) -> Dict[str, str]:
        return {k: v for k, v in self._lium.headers.items() if k != "X-API-KEY"}

    def _session_headers(self, workspace_id: Optional[str] = None) -> Dict[str, str]:
        if not self.session_token:
            raise LiumAuthError(NEEDS_SESSION)
        headers = {**self._plain_headers(), "Authorization": f"Bearer {self.session_token}"}
        if workspace_id:
            headers[WORKSPACE_HEADER] = workspace_id
        return headers

    def _read_headers(self) -> Optional[Dict[str, str]]:
        # a session lists every workspace of the account; a key lists the one it acts in
        return self._session_headers() if self.session_token else None

    def _session_request(self, method: str, endpoint: str, workspace_id: Optional[str] = None, **kwargs):
        try:
            return self._lium._request(method, endpoint, headers=self._session_headers(workspace_id), **kwargs)
        except LiumAuthError as e:
            if str(e) == NEEDS_SESSION:
                raise
            raise LiumAuthError("The session token was refused (expired?); run `lium workspaces login` again") from e

    # ------------------------------------------------------------------ reads (key or session)
    def list(self) -> List[WorkspaceInfo]:
        return [_info(d) for d in self._lium._request("GET", "/workspaces", headers=self._read_headers()).json()]

    def get(self, workspace_id: str) -> WorkspaceInfo:
        return _info(self._lium._request("GET", f"/workspaces/{workspace_id}", headers=self._read_headers()).json())

    def members(self, workspace_id: str) -> List[WorkspaceMember]:
        response = self._lium._request("GET", f"/workspaces/{workspace_id}/members", headers=self._read_headers())
        return [_member(d) for d in response.json()]

    def resolve(self, name_or_id: str) -> WorkspaceInfo:
        """A workspace by name (case-insensitive) or id among those this client can see."""
        for workspace in self.list():
            if name_or_id in (workspace.id, workspace.name) or name_or_id.lower() == workspace.name.lower():
                return workspace
        raise LiumError(f"No workspace named '{name_or_id}' is visible to this key or session")

    # ------------------------------------------------------------------ writes (session)
    def create(self, name: str) -> WorkspaceInfo:
        return _info(self._session_request("POST", "/workspaces", json={"name": name}).json())

    def invite(self, workspace_id: str, email: str, role: str = "member") -> Dict[str, Any]:
        """``POST /workspaces/{id}/invitations``: the address gets a link, an account or not (DAH-3031)."""
        return self._session_request(
            "POST", f"/workspaces/{workspace_id}/invitations", json={"email": email, "role": role}
        ).json()

    def remove_member(self, workspace_id: str, user_id: str) -> Dict[str, Any]:
        return self._session_request("DELETE", f"/workspaces/{workspace_id}/members/{user_id}").json()

    def transfer_billing(self, workspace_id: str, user_id: str) -> WorkspaceInfo:
        return _info(
            self._session_request(
                "POST", f"/workspaces/{workspace_id}/billing-owner/transfer", json={"user_id": user_id}
            ).json()
        )

    def delete(self, workspace_id: str) -> Dict[str, Any]:
        return self._session_request("DELETE", f"/workspaces/{workspace_id}").json()

    # ------------------------------------------------------------------ API keys (session)
    def list_keys(self, workspace_id: str) -> List[Dict[str, Any]]:
        return self._session_request("GET", "/keys", workspace_id).json()

    def create_key(self, name: str, workspace_id: str) -> Dict[str, Any]:
        """``POST /keys`` in the named workspace: the key is bound to it for good (DAH-2986)."""
        return self._session_request("POST", "/keys", workspace_id, json={"name": name}).json()


__all__ = ["WorkspacesClient", "WORKSPACE_HEADER", "NOT_ENABLED", "NEEDS_SESSION"]
