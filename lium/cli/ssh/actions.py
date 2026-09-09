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

        # The argument list comes from the pod's user, host and port, never from the
        # API's string as-is, and runs without a shell; the pod's host key is pinned.
        try:
            ssh_argv = lium.ssh_argv(pod)
        except ValueError as e:
            return ActionResult(ok=False, data={}, error=f"Pod '{pod.huid}': {e}")

        try:
            result = subprocess.run(ssh_argv, check=False)
        except KeyboardInterrupt:
            # Ctrl+C out of a session is the user's own doing, not a lium failure.
            ui.warning("\nSSH session interrupted")
            return ActionResult(ok=True, data={})

        # A non-zero remote shell is the remote's business; 255 is ssh itself.
        return ActionResult(ok=True, data={"exit_code": result.returncode})
