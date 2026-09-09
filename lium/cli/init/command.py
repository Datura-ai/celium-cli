"""Init command implementation."""

import click

from lium.cli.interactive import is_interactive
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
    \b
    Without a terminal on stdin (or with LIUM_NONINTERACTIVE=1) `lium init`
    behaves like `--no-browser`. Scripts can skip init entirely by setting
    LIUM_API_KEY.
    """

    # Step 2: verify a pending session
    if session:
        verify_action = VerifySessionAction(session_id=session)
        verify_result = verify_action.execute({})
        if not verify_result.ok:
            raise CliFailure("auth_failed", verify_result.error, EXIT_GENERAL_ERROR)
        _setup_ssh()
        return

    # Step 1 (headless): just print URL and exit. A browser nobody can see is
    # no use to a piped caller, so that case takes the headless path too.
    if no_browser or not is_interactive():
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
