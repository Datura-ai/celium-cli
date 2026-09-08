"""Configuration loading for the Lium SDK."""

import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def config_file_path() -> Path:
    return Path.home() / ".lium" / "config.ini"


def _read_config_file() -> ConfigParser:
    parser = ConfigParser()
    path = config_file_path()
    if path.exists():
        parser.read(path)
    return parser


def workspace_section(name: str) -> str:
    """The config.ini section holding one workspace's id and API key: ``[workspace.<name>]`` (lower-cased)."""
    return f"workspace.{name.lower()}"


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lium.io/api"
    base_pay_url: str = "https://pay-api.lium.io"
    ssh_key_path: Optional[Path] = None
    # The workspace this client was asked to act in (``--workspace`` / LIUM_WORKSPACE /
    # ``lium workspaces use``), by name or id; None when nothing was asked.
    workspace: Optional[str] = None
    # A browser-session token for the few routes that refuse API keys (``/workspaces`` writes,
    # ``/keys``): LIUM_SESSION_TOKEN or ``[session] token`` written by ``lium workspaces login``.
    session_token: Optional[str] = None

    @classmethod
    def load(cls, workspace: Optional[str] = None) -> "Config":
        """Load config from env/file with smart defaults.

        Key resolution, first match wins: the key configured for an explicitly requested workspace
        (``workspace=`` or LIUM_WORKSPACE, section ``[workspace.<name>]``), LIUM_API_KEY, the key of
        the workspace selected with ``lium workspaces use`` (``[workspaces] active``), ``[api] api_key``.
        A server without workspaces never sees a difference: the first and third rules only apply
        when such a section exists.
        """
        file_config = _read_config_file()
        requested = workspace or os.getenv("LIUM_WORKSPACE") or None
        active = file_config.get("workspaces", "active", fallback=None)

        api_key = None
        if requested:
            api_key = file_config.get(workspace_section(requested), "api_key", fallback=None)
        if not api_key:
            api_key = os.getenv("LIUM_API_KEY")
        if not api_key and not requested and active:
            api_key = file_config.get(workspace_section(active), "api_key", fallback=None)
        if not api_key:
            api_key = file_config.get("api", "api_key", fallback=None)

        if not api_key:
            raise ValueError("No API key found. Set LIUM_API_KEY or ~/.lium/config.ini")

        # Find SSH key with fallback
        ssh_key = None
        for key_name in ["id_ed25519", "id_rsa", "id_ecdsa"]:
            key_path = Path.home() / ".ssh" / key_name
            if key_path.exists():
                ssh_key = key_path
                break

        return cls(
            api_key=api_key,
            base_url=os.getenv("LIUM_BASE_URL", "https://lium.io/api"),
            base_pay_url=os.getenv("LIUM_PAY_URL", "https://pay-api.lium.io"),
            ssh_key_path=ssh_key,
            workspace=requested or active,
            session_token=os.getenv("LIUM_SESSION_TOKEN") or file_config.get("session", "token", fallback=None),
        )

    @property
    def ssh_public_keys(self) -> List[str]:
        """Get SSH public keys."""
        if not self.ssh_key_path:
            return []
        pub_path = self.ssh_key_path.with_suffix('.pub')
        if pub_path.exists():
            with open(pub_path) as f:
                return [line.strip() for line in f if line.strip().startswith(('ssh-', 'ecdsa-'))]
        return []


__all__ = ["Config", "config_file_path", "workspace_section"]
