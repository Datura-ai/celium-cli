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
from lium.cli.describe.actions import pod_detail
from lium.cli.describe.display import event_view, format_event
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
    last_event = None
    if pod_id:
        pod = next((p for p in pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)
        if not pod:
            raise CliFailure(
                "pod_not_found",
                f"Pod '{pod_id}' not found. If it was deleted, 'lium describe <pod id>' shows its last events.",
                EXIT_POD_NOT_FOUND,
            )
        pods = [pod]
        # One pod asked for by name: worth the extra call for why it is REBOOT_FAILED / BROKEN.
        last_event = event_view(pod_detail(lium, pod.id).get("last_event"))
    else:
        # Only a full listing defines what "pod 1" means; a filtered one does not.
        store_pod_selection(pods)

    # Always say which account answered. Two shells can hold different keys (a stale
    # LIUM_API_KEY vs ~/.lium/config.ini), and a pod list without its account is ambiguous —
    # an empty one even reads as an outage. JSON stays a bare array for jq; the line goes to stderr.
    key_config = getattr(lium, "config", None)
    account = f"Account: {key_config.api_key_description}" if key_config is not None else None

    if output_format == "json":
        if account:
            click.echo(account, err=True)
        payload = [
            display.compact_pod(p, index=None if pod_id else position)
            for position, p in enumerate(pods, start=1)
        ]
        if pod_id:
            payload[0]["last_event"] = last_event
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not pods:
        ui.warning("No active pods")
        if account:
            ui.dim(account)
        return

    # Build table
    table, header = display.build_pods_table(pods, show_index=not pod_id)

    # Display
    ui.info(header)
    ui.print(table)
    if last_event:
        ui.dim(f"last event: {format_event(last_event)}")
    if account:
        ui.dim(account)
