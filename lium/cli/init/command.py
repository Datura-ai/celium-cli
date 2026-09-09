"""Init command implementation."""

import json
import os

import click

from lium.cli import ui
from lium.cli.interactive import is_interactive
from lium.cli.settings import config
from lium.cli.utils import CliFailure, EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR, handle_errors
from .actions import (
    SaveApiKeyAction,
    SetupApiKeyAction,
    RequestAuthUrlAction,
    VerifySessionAction,
    SetupSshKeyAction,
)

KEYS_URL = "https://lium.io/api-keys"
HEADLESS_HINT = (
    f"No browser? Pass the key: 'lium init --api-key <key>' (create one at {KEYS_URL}), "
    "or export LIUM_API_KEY and skip init."
)
# What to do next when the key passed with --api-key is not saved; nothing was written, so the
# generic hints ("'lium config get api.api_key' shows which one is used") would point at nothing.
_FLAG_KEY_HINTS = {
    "invalid_api_key": "The key passed with --api-key was refused and nothing was saved; "
                       f"create one at {KEYS_URL} and pass it again",
    "api_unreachable": "Nothing was saved; check the network (or LIUM_BASE_URL) and run the same command again",
    "empty_api_key": "Nothing was saved; --api-key needs the key itself (an unset shell variable expands to nothing)",
}


@click.command("init")
@click.option("--api-key", "api_key", default=None, metavar="KEY",
              help=f"Save this API key (from {KEYS_URL}) after checking it works — no browser, no prompt.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Print auth URL instead of opening browser (step 1 of headless auth).")
@click.option("--session", default=None,
              help="Verify auth session and save API key (step 2 of headless auth).")
@click.option("--json", "json_output", is_flag=True,
              help="Print the result as machine-readable JSON (with --api-key or LIUM_API_KEY; the browser flows print for a person).")
@handle_errors
def init_command(api_key: str | None, no_browser: bool, session: str | None, json_output: bool):
    """Initialize Lium CLI configuration.

    Sets up API key and SSH key configuration. With --api-key (or LIUM_API_KEY
    already exported) nothing opens a browser and nothing waits for a person.

    \b
    Examples:
      lium init --api-key sk_...      # headless: check the key, save it, set up the SSH key
      lium init                       # opens browser for auth
      lium init --no-browser          # prints auth URL + session ID
      lium init --session <ID>        # verifies session and saves API key
    \b
    Without a terminal on stdin (or with LIUM_NONINTERACTIVE=1) `lium init`
    behaves like `--no-browser`. Scripts can skip init entirely by setting
    LIUM_API_KEY, or run `lium init --api-key <key>` once to save a key
    without a browser.
    """
    if api_key is not None and (session or no_browser):
        raise CliFailure(
            "invalid_arguments",
            "--api-key already provides the key; drop --no-browser / --session.",
            EXIT_CONFIGURATION_ERROR,
        )
    if json_output and api_key is None and not _env_key_name():
        # the browser flows talk to a person (URLs, "waiting…"); a machine caller has a key
        raise CliFailure(
            "invalid_arguments",
            "--json needs --api-key or an exported LIUM_API_KEY; the browser flows print for a person.",
            EXIT_CONFIGURATION_ERROR,
        )

    # Headless: the caller has a key — check it, save it, no browser. `is not None`: an empty
    # value (an unset $KEY) is an error to report, not a reason to start the browser flow.
    if api_key is not None:
        save_result = SaveApiKeyAction(api_key=api_key).execute({})
        if not save_result.ok:
            code = save_result.data.get("code", "invalid_api_key")
            exit_code = EXIT_CONFIGURATION_ERROR if code == "empty_api_key" else EXIT_API_ERROR
            # the generic hints point at the saved key ('lium config get api.api_key'); the key
            # checked here came from the flag and nothing was saved
            raise CliFailure(code, save_result.error, exit_code, hint=_FLAG_KEY_HINTS[code])
        ssh_path = _setup_ssh()
        _report("flag", ssh_path, json_output)
        return

    # LIUM_API_KEY is exported: every command already uses it (the environment wins over the
    # config file; the SDK reads this variable and no other), so there is nothing to
    # authenticate — with --session or --no-browser included, where the existing actions
    # would silently do nothing. Say so instead of doing only the SSH half in silence.
    if _env_key_name():
        ssh_path = _setup_ssh()
        _report("env", ssh_path, json_output)
        return

    # The CLI-only alias is read by `lium config` but not by the SDK that every command goes
    # through: an init that accepted it would exit 0 and leave `lium ps` with no key.
    if os.environ.get("LIUM_API_API_KEY") and not _file_key():
        raise CliFailure(
            "unsupported_env_key",
            "LIUM_API_API_KEY is read by the CLI config only; the commands read LIUM_API_KEY. "
            "Export LIUM_API_KEY instead, or run 'lium init --api-key <key>'.",
            EXIT_CONFIGURATION_ERROR,
        )

    # Step 2: verify a pending session
    if session:
        verify_action = VerifySessionAction(session_id=session)
        verify_result = verify_action.execute({})
        if not verify_result.ok:
            raise CliFailure("auth_failed", verify_result.error, EXIT_GENERAL_ERROR)
        ssh_path = _setup_ssh()
        _report("config" if verify_result.data.get("already_configured") else "session", ssh_path, json_output)
        return

    # Step 1 (headless): just print URL and exit. A browser nobody can see is
    # no use to a piped caller, so that case takes the headless path too.
    if no_browser or not is_interactive():
        url_action = RequestAuthUrlAction()
        url_result = url_action.execute({})
        if url_result.data.get("already_configured"):
            # a piped `lium init` next to a saved key: say where the key is instead of silence
            ssh_path = _setup_ssh()
            _report("config", ssh_path, json_output)
        return

    # Default: browser flow
    api_action = SetupApiKeyAction()
    api_result = api_action.execute({})

    if not api_result.ok:
        raise CliFailure("auth_failed", f"{api_result.error}. {HEADLESS_HINT}", EXIT_GENERAL_ERROR)

    ssh_path = _setup_ssh()
    _report("config" if api_result.data.get("already_configured") else "browser", ssh_path, json_output)


def _file_key() -> str | None:
    """The key in ~/.lium/config.ini itself — not what the environment overrides it with."""
    return config.get_all().get("api", {}).get("api_key")


def _env_key_name() -> str | None:
    """`LIUM_API_KEY` when it is exported — the one variable both the CLI and the SDK read."""
    return "LIUM_API_KEY" if os.environ.get("LIUM_API_KEY") else None


def _setup_ssh() -> str:
    """Setup SSH key (shared by every flow); returns the configured key path."""
    ssh_action = SetupSshKeyAction()
    ssh_result = ssh_action.execute({})
    if not ssh_result.ok:
        raise CliFailure("ssh_key_setup_failed", ssh_result.error, EXIT_GENERAL_ERROR)
    return config.get("ssh.key_path") or ""


def _report(saved_from: str, ssh_key_path: str, json_output: bool) -> None:
    """One line per fact the caller needs next: where the key came from, where it lives, which SSH key.

    ``saved_from`` is init's own word for how this run got the key (flag/env/config/session/browser);
    ``api_key_source`` in the JSON is the same value ``lium whoami --json`` and ``lium balance --json``
    print — where the next command will read the key from (``env:LIUM_API_KEY`` or ``config:<path> …``).
    """
    config_path = str(config.get_config_path())
    env_name = _env_key_name()
    if json_output:
        click.echo(json.dumps({
            "ok": True,
            "api_key_source": config.get_source("api.api_key"),
            "saved_from": saved_from,
            "env_key": env_name,          # set ⇒ this variable wins over the saved key while exported
            "config_path": config_path,
            "ssh_key_path": ssh_key_path,
        }, sort_keys=True))
        return
    if saved_from == "flag":
        ui.success(f"API key checked and saved to {config_path}")
        if env_name:
            ui.warning(f"{env_name} is set and wins over the saved key while it is exported")
    elif saved_from == "env":
        ui.info(f"Using the API key from {env_name}; the key is not written to {config_path}")
    elif saved_from == "config":
        ui.info(f"API key already saved in {config_path}")
    if ssh_key_path:
        ui.info(f"SSH key: {ssh_key_path}")
