"""`lium doctor`: is this machine, this account and (optionally) this pod ready to work?"""

import json
from typing import Optional

import click
from rich.markup import escape

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_POD_NOT_FOUND, handle_errors, parse_targets
from lium.cli.whoami.identity import collect_identity
from . import checks as check_module
from .checks import FAIL, OK, WARN

_MARK = {OK: "[success]ok[/success]", WARN: "[warning]warn[/warning]", FAIL: "[error]FAIL[/error]"}


def _render(checks) -> None:
    width = max(len(c.name) for c in checks)
    for check in checks:
        line = f"{_MARK[check.status]:<8} [dim]{check.name.ljust(width)}[/dim]  {escape(check.detail)}"
        ui.print(line)
        if check.hint and check.status != OK:
            ui.print(f"         {' ' * width}  [dim]→ {escape(check.hint)}[/dim]")


@click.command("doctor")
@click.argument("pod", required=False)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def doctor_command(pod: Optional[str], json_output: bool):
    """Check the local setup and the account; with a POD, also that pod.

    Reports the API key and its source, API reachability, balance, the SSH key
    and whether it is registered, the ssh/rsync clients and the CLI version.
    With a pod: its status, whether its SSH port accepts connections, and
    whether the template's CUDA build matches the GPU (a Blackwell node with a
    cu126 image will not run CUDA kernels).

    \b
    Exit status: 0 when nothing failed (warnings allowed), 1 otherwise.
    \b
    Examples:
      lium doctor
      lium doctor my-pod
      lium doctor --json | jq '.checks[] | select(.status != "ok")'
    """
    identity = collect_identity() if json_output else ui.load("Checking", collect_identity)
    checks = check_module.identity_checks(identity)

    pod_info = None
    if pod:
        if not identity.api_reachable:
            checks.append(check_module.Check("pod", FAIL, f"cannot look up {pod}: API not reachable"))
        else:
            pods = Lium().ps()
            matches = parse_targets(pod, pods)
            if not matches:
                raise CliFailure("pod_not_found", f"No pods match targets: {pod}", EXIT_POD_NOT_FOUND)
            if len(matches) > 1:
                raise CliFailure("pod_not_found", f"'{pod}' matches {len(matches)} pods; name exactly one", EXIT_POD_NOT_FOUND)
            pod_info = matches[0]
            checks += (
                check_module.pod_checks(pod_info) if json_output
                else ui.load(f"Checking pod {pod_info.name or pod_info.huid}", lambda: check_module.pod_checks(pod_info))
            )

    status = check_module.overall(checks)
    if json_output:
        click.echo(json.dumps({
            "ok": status != FAIL,
            "status": status,
            "checks": [c.to_dict() for c in checks],
            "identity": identity.to_dict(),
            "pod": pod_info.huid if pod_info else None,
        }, sort_keys=True))
    else:
        _render(checks)
        summary = {OK: "Everything looks good", WARN: "Ready, with warnings", FAIL: "Something needs fixing"}[status]
        (ui.success if status == OK else ui.warning if status == WARN else ui.error)(summary)

    if status == FAIL:
        raise SystemExit(1)
