"""Configuration loading for the Lium SDK."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

API_KEY_ENV_VAR = "LIUM_API_KEY"
# The CLI's generic ``LIUM_<SECTION>_<OPTION>`` spelling of ``[api] api_key``;
# ``ConfigManager.get`` checks it before ``LIUM_API_KEY``, so the SDK does too.
API_KEY_SECTION_ENV_VAR = "LIUM_API_API_KEY"
API_KEY_SOURCE_EXPLICIT = "explicit"
SSH_KEY_ENV_VAR = "LIUM_SSH_KEY_PATH"
DEFAULT_SSH_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")


def config_file_path() -> Path:
    return Path.home() / ".lium" / "config.ini"


def _config_option(section: str, option: str) -> Optional[str]:
    """``[section] option`` from the CLI's config file, or None."""
    config_file = config_file_path()
    if not config_file.exists():
        return None
    from configparser import ConfigParser

    config = ConfigParser()
    config.read(config_file)
    return config.get(section, option, fallback=None) or None


def resolve_api_key() -> Tuple[Optional[str], Optional[str]]:
    """The API key the SDK will use and where it came from.

    Returns ``(api_key, source)``; both are ``None`` when no key is configured.
    The order — ``LIUM_API_API_KEY``, ``LIUM_API_KEY``, then ``[api] api_key``
    in the config file — is the CLI's (``ConfigManager.get``), so ``lium
    config get api.api_key`` and the key the SDK sends are the same key.
    ``source`` is ``env:<VAR>`` or ``config:<path> [api] api_key``; it is
    recorded because two commands run in different shells can pick up
    different keys, and an auth error that does not say which key it used
    sends the caller to the wrong place.
    """
    for env_var in (API_KEY_SECTION_ENV_VAR, API_KEY_ENV_VAR):
        api_key = os.getenv(env_var)
        if api_key:
            return api_key, f"env:{env_var}"

    api_key = _config_option("api", "api_key")
    if api_key:
        return api_key, f"config:{config_file_path()} [api] api_key"

    return None, None


def resolve_ssh_key_path() -> Tuple[Optional[Path], Optional[str]]:
    """The private SSH key the SDK will use and where it came from.

    Order: ``LIUM_SSH_KEY_PATH``, then ``[ssh] key_path`` in the config file
    (what ``lium init`` writes), then the first of ``~/.ssh/id_ed25519``,
    ``id_rsa``, ``id_ecdsa`` that exists. A configured path is returned even
    when the file is missing, so the failure names the key the user chose
    instead of silently using another one. ``source`` is ``env:<VAR>``,
    ``config:<path> [ssh] key_path`` or ``default:<path>``.
    """
    configured = os.getenv(SSH_KEY_ENV_VAR)
    if configured:
        return Path(configured).expanduser(), f"env:{SSH_KEY_ENV_VAR}"

    configured = _config_option("ssh", "key_path")
    if configured:
        return Path(configured).expanduser(), f"config:{config_file_path()} [ssh] key_path"

    for key_name in DEFAULT_SSH_KEY_NAMES:
        key_path = Path.home() / ".ssh" / key_name
        if key_path.exists():
            return key_path, f"default:{key_path}"

    return None, None


def api_key_fingerprint(api_key: Optional[str]) -> str:
    """A short, non-secret handle for a key: first six and last four characters."""
    if not api_key:
        return "none"
    if len(api_key) <= 12:
        return "***"
    return f"{api_key[:6]}…{api_key[-4:]}"


@dataclass
class Config:
    api_key: str
    base_url: str = "https://lium.io/api"
    base_pay_url: str = "https://pay-api.lium.io"
    ssh_key_path: Optional[Path] = None
    api_key_source: str = API_KEY_SOURCE_EXPLICIT
    ssh_key_source: Optional[str] = None

    @classmethod
    def load(cls) -> "Config":
        """Load config from env/file, in the same order the CLI uses."""
        api_key, source = resolve_api_key()

        if not api_key:
            raise ValueError(f"No API key found. Set {API_KEY_ENV_VAR} or {config_file_path()}")

        ssh_key, ssh_source = resolve_ssh_key_path()

        return cls(
            api_key=api_key,
            base_url=os.getenv("LIUM_BASE_URL", "https://lium.io/api"),
            base_pay_url=os.getenv("LIUM_PAY_URL", "https://pay-api.lium.io"),
            ssh_key_path=ssh_key,
            api_key_source=source or API_KEY_SOURCE_EXPLICIT,
            ssh_key_source=ssh_source,
        )

    @property
    def api_key_fingerprint(self) -> str:
        return api_key_fingerprint(self.api_key)

    @property
    def api_key_description(self) -> str:
        """``key <fingerprint> from <source>`` — what an auth error should name."""
        return f"key {self.api_key_fingerprint} from {self.api_key_source}"

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


__all__ = [
    "Config",
    "API_KEY_ENV_VAR",
    "API_KEY_SECTION_ENV_VAR",
    "SSH_KEY_ENV_VAR",
    "api_key_fingerprint",
    "resolve_api_key",
    "resolve_ssh_key_path",
]
