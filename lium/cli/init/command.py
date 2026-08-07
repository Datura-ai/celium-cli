"""Init command implementation."""

import click

from lium.cli.settings import config
from lium.cli.utils import CliFailure, EXIT_GENERAL_ERROR, handle_errors
from .actions import SetupApiKeyAction, RequestAuthUrlAction, VerifySessionAction, SetupSshKeyAction


@click.command("init")
@click.option("--no-browser", is_flag=True, default=False,
              help="Print auth URL instead of opening browser (step 1 of headless auth).")
@click.option("--session", default=None,
              help="Verify auth session and save API key (step 2 of headless auth).")
@handle_errors
def init_command(no_browser: bool, session: str | None):
    """Initialize Lium CLI configuration.

    Sets up API key and SSH key configuration.

    \b
    Examples:
      lium init                     # opens browser for auth
      lium init --no-browser        # prints auth URL + session ID
      lium init --session <ID>      # verifies session and saves API key
    """

    # Step 2: verify a pending session
    if session:
        verify_action = VerifySessionAction(session_id=session)
        verify_result = verify_action.execute({})
        if not verify_result.ok:
            raise CliFailure("auth_failed", verify_result.error, EXIT_GENERAL_ERROR)
        _setup_ssh()
        return

    # Step 1 (headless): just print URL and exit
    if no_browser:
        url_action = RequestAuthUrlAction()
        url_action.execute({})
        return

    # Default: browser flow
    api_action = SetupApiKeyAction()
    api_result = api_action.execute({})

    if not api_result.ok:
        raise CliFailure("auth_failed", api_result.error, EXIT_GENERAL_ERROR)

    _setup_ssh()


def _setup_ssh():
    """Setup SSH key (shared by browser and headless flows)."""
    ssh_action = SetupSshKeyAction()
    ssh_result = ssh_action.execute({})
    if not ssh_result.ok:
        raise CliFailure("ssh_key_setup_failed", ssh_result.error, EXIT_GENERAL_ERROR)
