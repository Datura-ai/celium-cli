import subprocess

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo
from lium.cli import ui


class SshAction:
    """Execute SSH connection."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute SSH to pod."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]

        try:
            ssh_cmd = lium.ssh(pod)
        except ValueError:
            ssh_cmd = pod.ssh_cmd

        ssh_cmd += " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

        try:
            result = subprocess.run(ssh_cmd, shell=True, check=False)
        except KeyboardInterrupt:
            # Ctrl+C out of a session is the user's own doing, not a lium failure.
            ui.warning("\nSSH session interrupted")
            return ActionResult(ok=True, data={})

        # A non-zero remote shell is the remote's business; 255 is ssh itself.
        return ActionResult(ok=True, data={"exit_code": result.returncode})
