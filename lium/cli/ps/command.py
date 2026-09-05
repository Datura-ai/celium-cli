"""Pods (ps) command implementation."""

import json
import time
from typing import List, Optional, Tuple

import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    console,
    handle_errors,
    ensure_config,
)
from . import display, selection
from .actions import GetPodsAction

# Below this many columns the Ports column makes every row wrap; drop it unless --wide.
WIDE_TERMINAL_COLUMNS = 120


def _terminal_width() -> int:
    try:
        return int(console.size.width) or WIDE_TERMINAL_COLUMNS
    except Exception:  # noqa: BLE001 - no terminal at all
        return WIDE_TERMINAL_COLUMNS


def _load_pods(lium: Lium, quiet: bool) -> List[PodInfo]:
    action = GetPodsAction()
    ctx = {"lium": lium}
    if quiet:
        return action.execute(ctx).data["pods"]
    return ui.load("Loading pods", lambda: action.execute(ctx)).data["pods"]


def _select(pods: List[PodInfo], pod_id: Optional[str], filters, sort_key: Optional[str], reverse: bool) -> List[PodInfo]:
    if pod_id:
        pod = next((p for p in pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)
        if not pod:
            raise CliFailure("pod_not_found", f"Pod '{pod_id}' not found", EXIT_POD_NOT_FOUND)
        pods = [pod]
    pods = [p for p in pods if selection.matches(p, filters)]
    return selection.sort_pods(pods, sort_key, reverse)


def _render(pods: List[PodInfo], output_format: str, wide: bool, filtered: bool) -> None:
    if output_format == "json":
        click.echo(json.dumps([display.compact_pod(p) for p in pods], indent=2, ensure_ascii=False))
        return
    if not pods:
        ui.warning("No pods match the filters" if filtered else "No active pods")
        return
    short = not wide and _terminal_width() < WIDE_TERMINAL_COLUMNS
    table, header = display.build_pods_table(pods, short=short)
    ui.info(header)
    ui.print(table)
    if short:
        ui.dim("Ports hidden on a narrow terminal; use --wide or --format json")


@click.command("ps")
@click.argument("pod_id", required=False)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_flag", is_flag=True, help="Same as --format json")
@click.option("--sort", "sort_key", type=click.Choice(selection.SORT_KEYS), help="Sort rows")
@click.option("--reverse", "-r", is_flag=True, help="Reverse the sort order")
@click.option(
    "--filter", "filters", multiple=True, metavar="KEY=VALUE",
    help=f"Keep pods whose field starts with VALUE (case-insensitive); repeatable. KEY: {', '.join(selection.FILTER_KEYS)}",
)
@click.option("--watch", "-w", type=float, metavar="SECONDS", help="Refresh every N seconds until interrupted")
@click.option("--wide", is_flag=True, help="Always show every column, including ports")
@handle_errors
def ps_command(
    pod_id: Optional[str],
    output_format: str,
    json_flag: bool,
    sort_key: Optional[str],
    reverse: bool,
    filters: Tuple[str, ...],
    watch: Optional[float],
    wide: bool,
):
    """List active GPU pods.

    \b
    Examples:
      lium ps                                  # table; ports hidden on narrow terminals
      lium ps --wide                           # every column
      lium ps --format json | jq '.[].huid'
      lium ps my-pod --format json             # one pod
      lium ps --filter status=RUNNING --sort spent
      lium ps --filter gpu=H100 --filter name=train
      lium ps --watch 10                       # refresh every 10 s
    """
    if json_flag:
        output_format = "json"
    if watch is not None and watch <= 0:
        raise CliFailure("invalid_arguments", "--watch must be a positive number of seconds", EXIT_CONFIGURATION_ERROR)
    try:
        parsed_filters = selection.parse_filters(filters)
    except ValueError as exc:
        raise CliFailure("invalid_arguments", str(exc), EXIT_CONFIGURATION_ERROR)

    ensure_config()
    lium = Lium()

    def once(quiet: bool) -> None:
        pods = _select(_load_pods(lium, quiet), pod_id, parsed_filters, sort_key, reverse)
        _render(pods, output_format, wide, filtered=bool(parsed_filters))

    once(quiet=output_format == "json")
    if watch is None:
        return
    try:
        while True:
            time.sleep(watch)
            if output_format != "json":
                click.clear()
            once(quiet=True)
    except KeyboardInterrupt:
        return
