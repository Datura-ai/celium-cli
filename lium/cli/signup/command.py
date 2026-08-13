"""Signup command implementation."""

import json

import click

from lium.cli import ui
from lium.cli.init.actions import SetupSshKeyAction
from lium.cli.utils import CliFailure, handle_errors
from .actions import AttachEmailAction, KeySignupAction, SignupAction, generate_password


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
@click.option("--password", default=None, envvar="LIUM_SIGNUP_PASSWORD",
              help="Account password (generated when omitted). Falls back to LIUM_SIGNUP_PASSWORD.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def signup_command(email: str, display_name: str | None, password: str | None, json_output: bool):
    """Create a Lium account and store the API key it mints.

    Non-interactive: safe to run from an agent. The API key is written to
    ~/.lium/config.ini, so `lium ls` and `lium up` work right after.

    To set your own password, prefer LIUM_SIGNUP_PASSWORD over --password:
    a flag value is left behind in the shell history and in `ps` output.

    \b
    Examples:
      lium signup --email ada@example.com
      lium signup --email ada@example.com --json
      LIUM_SIGNUP_PASSWORD=... lium signup --email ada@example.com
    """
    password = password or generate_password()
    display_name = display_name or email.split("@")[0]

    signup_result = SignupAction(email=email, password=password, display_name=display_name).execute({})
    if not signup_result.ok:
        # the account may already exist server-side; losing the generated password would make it unreachable
        if signup_result.data.get("account_may_exist"):
            raise CliFailure(
                "signup_failed",
                f"{signup_result.error} Log in at https://lium.io with "
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
                _credit_line(credit_granted),
                "Then: lium ls, lium up <node-id>.",
                "The verification link in the confirmation email does not gate renting — it confirms "
                "the address so password resets and account emails reach the user.",
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
    ui.print(f"  1. {_credit_line(credit_granted)}")
    ui.print("  2. Then 'lium ls' and 'lium up <node-id>'.")
    ui.print("")
    ui.dim("  The verification link in the confirmation email does not gate renting — it confirms")
    ui.dim("  the address so password resets and account emails reach you.")


@click.command("signup-key")
@click.option("--coldkey", required=True, help="Bittensor coldkey (wallet) name holding the hotkey.")
@click.option("--hotkey", required=True, help="Bittensor hotkey name — its address owns the account.")
@click.option("--name", "display_name", default=None, help="Display name (defaults to the address).")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def signup_key_command(coldkey: str, hotkey: str, display_name: str | None, json_output: bool):
    """Create a Lium account owned by a Bittensor hotkey and store the API key it mints.

    No email, no password: the hotkey signs a challenge from the backend and the
    account is created for its address. The wallet must already exist under
    ~/.bittensor/wallets — this command never generates a key. Add an email
    login later with 'lium attach-email'.

    \b
    Examples:
      lium signup-key --coldkey default --hotkey agent
      lium signup-key --coldkey default --hotkey agent --name agent-01 --json
    """
    signup_result = KeySignupAction(
        coldkey=coldkey, hotkey=hotkey, display_name=display_name
    ).execute({})
    if not signup_result.ok:
        raise CliFailure("signup_key_failed", signup_result.error)

    ssh_result = SetupSshKeyAction().execute({})
    credit_granted = signup_result.data.get("signup_credit_granted")
    next_steps = [
        _credit_line(credit_granted),
        "Add an email login with 'lium attach-email --email you@example.com'.",
        "Then: lium ls, lium up <node-id>.",
    ]

    if json_output:
        click.echo(json.dumps({
            "address": signup_result.data["address"],
            "api_key": signup_result.data["api_key"],
            "is_new_user": signup_result.data.get("is_new_user"),
            "signup_credit_granted": credit_granted,
            "ssh_key_configured": ssh_result.ok,
            "next_steps": next_steps,
        }, sort_keys=True))
        return

    ui.success(f"Account created for {signup_result.data['address']}")
    ui.info("API key stored in ~/.lium/config.ini")
    if not ssh_result.ok:
        ui.warning(f"SSH key not configured: {ssh_result.error}")

    ui.print("")
    ui.info("Next steps:")
    for number, step in enumerate(next_steps, start=1):
        ui.print(f"  {number}. {step}")


@click.command("attach-email")
@click.option("--email", required=True, help="The user's real email — the verification link is sent there.")
@click.option("--password", default=None, envvar="LIUM_SIGNUP_PASSWORD",
              help="Account password (generated when omitted). Falls back to LIUM_SIGNUP_PASSWORD.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def attach_email_command(email: str, password: str | None, json_output: bool):
    """Add an email + password login to the account of the stored API key.

    Meant for an account created with 'lium signup-key', which has no email at
    all. The address stays unverified until the user clicks the link in the
    confirmation email.

    To set your own password, prefer LIUM_SIGNUP_PASSWORD over --password:
    a flag value is left behind in the shell history and in `ps` output.

    \b
    Examples:
      lium attach-email --email ada@example.com
      LIUM_SIGNUP_PASSWORD=... lium attach-email --email ada@example.com --json
    """
    password = password or generate_password()

    attach_result = AttachEmailAction(email=email, password=password).execute({})
    if not attach_result.ok:
        # the credential may be attached server-side; losing the generated password would strand that login
        if attach_result.data.get("credential_may_exist"):
            raise CliFailure(
                "attach_email_failed",
                f"{attach_result.error} Log in at https://lium.io with {email} / {password} to check.",
                data={"email": email, "password": password},
            )
        raise CliFailure("attach_email_failed", attach_result.error)

    if json_output:
        click.echo(json.dumps({
            "email": email,
            "password": password,
            "next_steps": [
                "Ask the user to click the verification link in the confirmation email.",
                "The account keeps its API key — nothing else changes.",
            ],
        }, sort_keys=True))
        return

    ui.success(f"Email {email} attached to the account")
    ui.print(f"\n  password: {password}")
    ui.dim("  Save it — it is the dashboard login at https://lium.io\n")
    ui.info("Click the verification link in the confirmation email to verify the address.")
