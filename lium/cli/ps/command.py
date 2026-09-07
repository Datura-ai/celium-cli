"""Pods (ps) command implementation."""

import json
from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    ensure_config,
    store_pod_selection,
)
from . import display
from .actions import GetPodsAction


@click.command("ps")
@click.argument("pod_id", required=False)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@handle_errors
def ps_command(pod_id: Optional[str], output_format: str):
    """List active GPU pods.

    \b
    The # column (and "index" in --format json) is the row number that rm, ssh,
    exec and scp accept in place of a pod huid. Those commands only honour it
    while the row still holds the same pod as this listing and for 10 minutes
    after it; the huid is the stable identifier for scripts.
    """

    ensure_config()

    # Load data
    lium = Lium()
    ctx = {"lium": lium}

    action = GetPodsAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading pods", lambda: action.execute(ctx))

    pods = result.data["pods"]

    # Filter by pod_id if provided
    if pod_id:
        pod = next((p for p in pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)
        if not pod:
            raise CliFailure("pod_not_found", f"Pod '{pod_id}' not found", EXIT_POD_NOT_FOUND)
        pods = [pod]
    else:
        # Only a full listing defines what "pod 1" means; a filtered one does not.
        store_pod_selection(pods)

    # Check if empty
    if not pods:
        if output_format == "json":
            click.echo("[]")
        else:
            ui.warning("No active pods")
        return

    if output_format == "json":
        payload = [
            display.compact_pod(p, index=None if pod_id else position)
            for position, p in enumerate(pods, start=1)
        ]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    # Build table
    table, header = display.build_pods_table(pods, show_index=not pod_id)

    # Display
    ui.info(header)
    ui.print(table)
