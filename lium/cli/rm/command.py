"""Remove (rm) command implementation."""

import sys
from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors
from . import validation, parsing
from .actions import RemovePodsAction, ScheduleRemovalAction


@click.command("rm")
@click.argument("targets", required=False)
@click.option("--all", "-a", is_flag=True, help="Remove all active pods")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.option("--in", "in_duration", help="Schedule removal after duration")
@click.option("--at", "at_time", help="Schedule removal at time")
@handle_errors
def rm_command(
    targets: Optional[str],
    all: bool,
    yes: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
):
    """Remove (terminate) GPU pods.

    \b
    Removal is irreversible. Exits non-zero when nothing matched TARGETS, so a
    typo cannot look like a successful teardown.
    """

    # Validate
    valid, error = validation.validate(targets, all, in_duration, at_time)
    if not valid:
        ui.error(error)
        raise SystemExit(2)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        ui.warning("No active pods")
        return

    # Parse
    parsed, error = parsing.parse(targets, all, all_pods, in_duration, at_time)
    if error:
        ui.error(error)
        raise SystemExit(2)

    selected_pods = parsed.get("selected_pods")

    if not selected_pods:
        ui.error(f"No pods match targets: {targets}")
        raise SystemExit(2)

    # --all on a shared account can wipe a colleague's work, so it asks once.
    if all and not yes and sys.stdin.isatty():
        listed = ", ".join(pod.huid for pod in selected_pods)
        click.confirm(f"Remove all {len(selected_pods)} pods ({listed})?", abort=True)
    termination_time = parsed.get("termination_time")

    # Execute
    ctx = {"pods": selected_pods, "lium": lium}

    if termination_time:
        ctx["termination_time"] = termination_time.isoformat()
        action = ScheduleRemovalAction()
    else:
        action = RemovePodsAction()

    result = action.execute(ctx)

    failed_huids = result.data.get("failed_huids", []) if not result.ok else []
    removed_huids = [pod.huid for pod in selected_pods if pod.huid not in failed_huids]

    # Say what happened: silence is indistinguishable from having done nothing.
    if removed_huids:
        verb = "Scheduled removal for" if termination_time else "Removed"
        ui.info(f"{verb} {len(removed_huids)} pod(s): {', '.join(removed_huids)}")

    if failed_huids:
        ui.error(f"Failed to remove pods: {', '.join(failed_huids)}")
        raise SystemExit(1)
