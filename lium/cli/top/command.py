"""`lium top`: GPU utilisation of one or more pods, sampled over SSH."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    parse_targets,
)
from . import display

# Sampling every pod one after another would make --all on ten pods take ten
# round trips; a handful of parallel SSH sessions keeps a sample under a second.
MAX_PARALLEL_PODS = 8


def sample(lium: Lium, pods: List[PodInfo]) -> List[Dict[str, Any]]:
    """One reading per pod, in the order given. A pod that fails is a row, not an abort."""

    def one(pod: PodInfo) -> Dict[str, Any]:
        try:
            return display.pod_reading(pod, gpus=lium.gpu_stats(pod))
        except Exception as exc:  # noqa: BLE001 - one unreachable pod must not hide the others
            return display.pod_reading(pod, error=str(exc))

    if len(pods) == 1:
        return [one(pods[0])]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_PODS, len(pods))) as executor:
        return list(executor.map(one, pods))


def _select_pods(lium: Lium, targets: Optional[str], all_pods: bool) -> List[PodInfo]:
    pods = ui.load("Loading pods", lambda: lium.ps())
    if all_pods:
        if not pods:
            raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)
        return pods
    if not targets:
        raise CliFailure(
            "invalid_arguments",
            "Name a pod (lium top <pod>) or pass --all",
            EXIT_CONFIGURATION_ERROR,
        )
    selected = parse_targets(targets, pods)
    if not selected:
        raise CliFailure("pod_not_found", f"No pods match targets: {targets}", EXIT_POD_NOT_FOUND)
    return selected


@click.command("top")
@click.argument("targets", required=False)
@click.option("--all", "all_pods", is_flag=True, help="Every active pod")
@click.option("--watch", "-w", type=float, metavar="SECONDS", help="Refresh every N seconds until interrupted")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' prints one JSON object per sample to stdout.",
)
@handle_errors
def top_command(targets: Optional[str], all_pods: bool, watch: Optional[float], output_format: str):
    """Show GPU utilisation, memory, temperature and power on pods.

    Runs nvidia-smi on each pod over SSH and prints one row per GPU.

    \b
    TARGETS: a pod name, huid, id or index from 'lium ps'; comma-separated
    for several; or --all.
    \b
    Examples:
      lium top my-pod
      lium top my-pod --watch 5
      lium top --all --format json | jq '.pods[].gpus[].utilization_pct'
    """
    if watch is not None and watch <= 0:
        raise CliFailure("invalid_arguments", "--watch must be a positive number of seconds", EXIT_CONFIGURATION_ERROR)

    lium = Lium()
    pods = _select_pods(lium, targets, all_pods)
    show_pod = len(pods) > 1

    def render(readings: List[Dict[str, Any]]) -> None:
        if output_format == "json":
            click.echo(json.dumps(display.to_json(readings), sort_keys=True))
            return
        ui.print(display.build_table(readings, show_pod=show_pod))
        ui.dim(display.summary_line(readings))

    def take_sample() -> List[Dict[str, Any]]:
        if output_format == "json":
            return sample(lium, pods)
        return ui.load("Sampling GPUs", lambda: sample(lium, pods))

    readings = take_sample()
    render(readings)
    if watch is None:
        if any(r.get("error") for r in readings):
            raise SystemExit(1)
        return

    try:
        while True:
            time.sleep(watch)
            readings = sample(lium, pods)
            if output_format != "json":
                click.clear()
            render(readings)
    except KeyboardInterrupt:
        return
