"""Signup command implementation."""

import json

import click

from lium.cli import ui
from lium.cli.init.actions import SetupSshKeyAction
from lium.cli.utils import CliFailure, handle_errors
from .actions import SignupAction, generate_password


def _credit_line(credit_granted: bool | None) -> str:
    # wording follows the backend's signup_credit_granted flag; older backends omit it, so nothing is asserted then
    if credit_granted is True:
        return "A $5 signup credit was granted — check it with 'lium balance'."
    if credit_granted is False:
        return ("No signup credit was granted — it is granted once per IP address and can be disabled. "
                "Fund the account before renting.")
    return "Check the balance with 'lium balance' and fund the account before renting."


@click.command("signup")
@click.option("--email", required=True, help="The user's real email — the verification link is sent there.")
@click.option("--name", "display_name", default=None, help="Display name (defaults to the email's local part).")
@click.option("--password", default=None, help="Account password (generated when omitted).")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def signup_command(email: str, display_name: str | None, password: str | None, json_output: bool):
    """Create a Lium account and store the API key it mints.

    Non-interactive: safe to run from an agent. The API key is written to
    ~/.lium/config.ini, so `lium ls` and `lium up` work right after.

    \b
    Examples:
      lium signup --email ada@example.com
      lium signup --email ada@example.com --json
    """
    password = password or generate_password()
    display_name = display_name or email.split("@")[0]

    signup_result = SignupAction(email=email, password=password, display_name=display_name).execute({})
    if not signup_result.ok:
        # the account may already exist server-side; losing the generated password would make it unreachable
        if signup_result.data.get("account_may_exist"):
            raise CliFailure(
                "signup_failed",
                f"{signup_result.error} The account may have been created — log in at https://lium.io with "
                f"{email} / {password} and copy your API key from the dashboard.",
                data={"email": email, "password": password},
            )
        raise CliFailure("signup_failed", signup_result.error)

    ssh_result = SetupSshKeyAction().execute({})
    credit_granted = signup_result.data.get("signup_credit_granted")

    if json_output:
        click.echo(json.dumps({
            "email": email,
            "password": password,
            "api_key": signup_result.data["api_key"],
            "ssh_key_configured": ssh_result.ok,
            "signup_credit_granted": credit_granted,
            "next_steps": [
                "Ask the user to click the verification link in the welcome email — renting is blocked until then.",
                _credit_line(credit_granted),
                "Then: lium ls, lium up <node-id>.",
            ],
        }, sort_keys=True))
        return

    ui.success(f"Account created for {email}")
    ui.print(f"\n  password: {password}")
    ui.dim("  Save it — it is the dashboard login at https://lium.io\n")
    ui.info("API key stored in ~/.lium/config.ini")
    if not ssh_result.ok:
        ui.warning(f"SSH key not configured: {ssh_result.error}")

    ui.print("")
    ui.info("Before the first rental:")
    ui.print("  1. Click the verification link in the welcome email — renting is blocked until then.")
    ui.print(f"  2. {_credit_line(credit_granted)}")
    ui.print("  3. Then 'lium ls' and 'lium up <node-id>'.")
