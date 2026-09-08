"""SSH command implementation."""

import shutil
import subprocess
from typing import List, Tuple
import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import handle_errors, parse_targets
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND, EXIT_SSH_ERROR
from . import validation, parsing
from .actions import SshAction


# ssh(1) uses 255 for its own connection failures; anything else is the remote
# shell's own exit status, which is not a failure of the lium command.
_SSH_CONNECTION_FAILED = 255


def get_ssh_method_and_pod(target: str) -> Tuple[List[str], PodInfo]:
    """The ssh argument list for a pod. Raises when SSH is not possible."""
    if not shutil.which("ssh"):
        raise CliFailure(
            "ssh_client_missing",
            "'ssh' command not found. Please install an SSH client.",
            EXIT_SSH_ERROR,
        )

    lium = Lium()
    all_pods = lium.ps()

    pods = parse_targets(target, all_pods)
    pod = pods[0] if pods else None

    if not pod:
        raise CliFailure("pod_not_found", f"No pods match targets: {target}", EXIT_POD_NOT_FOUND)

    if not pod.ssh_cmd:
        raise CliFailure(
            "ssh_unavailable",
            f"No SSH connection available for pod '{pod.huid}'",
            EXIT_SSH_ERROR,
        )

    # The pod's user, host and port become the argument list (no shell), with the
    # pinned host-key options; a value of any other shape is refused here.
    try:
        return lium.ssh_argv(pod), pod
    except ValueError as e:
        raise CliFailure("ssh_unavailable", f"Pod '{pod.huid}': {e}", EXIT_SSH_ERROR)


def ssh_session_connected(ssh_argv: List[str]) -> bool:
    """Open the session; False when the connection never opened.

    A remote shell exiting non-zero is the user's business, not a failure of the
    lium command — only ssh's own connection failure is.
    """
    try:
        result = subprocess.run(ssh_argv, check=False)

        if result.returncode not in (0, _SSH_CONNECTION_FAILED):
            ui.dim(f"\nSSH session ended with exit code {result.returncode}")
        return result.returncode != _SSH_CONNECTION_FAILED
    except KeyboardInterrupt:
        ui.warning("\nSSH session interrupted")
        return True
    except Exception as e:
        raise CliFailure("ssh_failed", f"Error executing SSH: {e}", EXIT_SSH_ERROR)


@click.command("ssh")
@click.argument("target")
@handle_errors
def ssh_command(target: str):
    """Open SSH session to a GPU pod.

    \b
    TARGET: Pod identifier - can be:
      - Pod huid, name or ID (eager-wolf-aa) — the stable form for scripts
      - Row number of your last 'lium ps' in this shell (1, 2) — refused if that pod is gone or the listing is >10 min old

    \b
    Examples:
      lium ssh 1                    # SSH to pod #1 from ps
      lium ssh eager-wolf-aa        # SSH to specific pod
    """

    # Validate
    valid, error = validation.validate(target)
    if not valid:
        if "'ssh' command not found" in error:
            raise CliFailure("ssh_client_missing", error, EXIT_SSH_ERROR)
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, failure = parsing.parse(target, all_pods)
    if failure:
        raise failure

    pod = parsed.get("pod")

    # Execute
    ctx = {"lium": lium, "pod": pod}

    action = SshAction()
    result = action.execute(ctx)
    if not result.ok:
        raise CliFailure("ssh_unavailable", result.error, EXIT_SSH_ERROR)

    exit_code = result.data.get("exit_code")
    if exit_code == _SSH_CONNECTION_FAILED:
        raise CliFailure(
            "ssh_failed",
            f"SSH connection to '{pod.huid}' failed",
            EXIT_SSH_ERROR,
        )
    if exit_code:
        ui.dim(f"SSH session ended with exit code {exit_code}")
