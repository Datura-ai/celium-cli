"""SSH command implementation."""

import shutil
import subprocess
from typing import Tuple
import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import handle_errors, parse_targets
from lium.cli.utils import CliFailure, EXIT_POD_NOT_FOUND, EXIT_SSH_ERROR
from . import validation, parsing
from .actions import SshAction


# ssh(1) uses 255 for its own connection failures; anything else is the remote
# shell's own exit status, which is not a failure of the lium command.
_SSH_CONNECTION_FAILED = 255


def get_ssh_method_and_pod(target: str) -> Tuple[str, PodInfo]:
    """The ssh command line for a pod. Raises when SSH is not possible."""
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

    try:
        ssh_cmd = lium.ssh(pod)
        ssh_cmd += " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        return ssh_cmd, pod
    except ValueError:
        ssh_cmd = pod.ssh_cmd + " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        return ssh_cmd, pod


def ssh_session_connected(ssh_cmd: str) -> bool:
    """Open the session; False when the connection never opened.

    A remote shell exiting non-zero is the user's business, not a failure of the
    lium command — only ssh's own connection failure is.
    """
    try:
        result = subprocess.run(ssh_cmd, shell=True, check=False)

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
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    \b
    Examples:
      lium ssh 1                    # SSH to pod #1 from ps
      lium ssh eager-wolf-aa        # SSH to specific pod
    """

    # Validate
    valid, error = validation.validate(target)
    if not valid:
        ui.error(error)
        return

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        ui.warning("No active pods")
        return

    # Parse
    parsed, error = parsing.parse(target, all_pods)
    if error:
        ui.error(error)
        return

    pod = parsed.get("pod")

    # Execute
    ctx = {"lium": lium, "pod": pod}

    action = SshAction()
    result = action.execute(ctx)

    if not result.ok:
        if result.error:
            ui.error(result.error)
        elif result.data.get("exit_code"):
            ui.dim(f"SSH session ended with exit code {result.data['exit_code']}")
