"""List (ls) command implementation."""

import json
from typing import Optional, List
import click

from lium.sdk import Lium, ExecutorInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    calculate_pareto_frontier,
    handle_errors,
    resolve_output_format,
    store_executor_selection,
)
from lium.cli.completion import get_gpu_completions
from . import validation, display
from .actions import GetExecutorsAction


def ls_store_executor(gpu_type: Optional[str] = None, sort_by: str = "download") -> List[ExecutorInfo]:
    """Load and store nodes without displaying them."""
    lium = Lium()
    executors = lium.ls(gpu_type=gpu_type)

    if not executors:
        return []

    pareto_flags = calculate_pareto_frontier(executors)
    executors_with_pareto = list(zip(executors, pareto_flags))

    executors_with_pareto = sorted(
        executors_with_pareto,
        key=lambda x: (not x[1], -x[0].download_speed)
    )

    sorted_executors = [e for e, _ in executors_with_pareto]
    store_executor_selection(sorted_executors)

    return sorted_executors


@click.command("ls", epilog="Use --format json for machine-readable output.")
@click.option("--gpu", "gpu_type", shell_complete=get_gpu_completions, help="Filter by GPU type, e.g. A100")
@click.option("--count", "gpu_count", type=int, help="Exact GPU count to match (e.g., 1, 8)")
@click.option("--min-cuda", "min_cuda_version", type=float, help="Minimum CUDA version, e.g. 12.4 (NVIDIA drivers are backward compatible)")
@click.option(
    "--nvlink",
    is_flag=True,
    default=False,
    help="Only nodes whose GPUs are all joined by NVLink (Link column NV#). Nodes with no topology report yet are excluded.",
)
@click.option(
    "--min-download",
    "--min-ingress",
    "min_download_mbps",
    type=float,
    default=None,
    help="Minimum ingress in Mbps, judged on the CDN probe (Net↓ column) when the node has one, else on Download (Mbps).",
)
@click.option("--lat", type=float, help="Latitude for distance filtering")
@click.option("--lon", type=float, help="Longitude for distance filtering")
@click.option("--max-distance", "max_distance", type=int, help="Maximum distance in miles from --lat/--lon")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(display.SORT_KEYS + list(display.SORT_KEY_ALIASES)),
    default=None,
    help="Sort result by the chosen field. An explicit --sort wins over the ★ optimal ordering.",
)
@click.option("--limit", type=int, default=None, help="Limit number of rows shown.")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def ls_command(
    gpu_type: Optional[str],
    gpu_count: Optional[int],
    lat: Optional[float],
    lon: Optional[float],
    max_distance: Optional[int],
    sort_by: Optional[str],
    limit: Optional[int],
    output_format: str,
    json_output: bool,
    min_cuda_version: Optional[float],
    nvlink: bool,
    min_download_mbps: Optional[float],
):
    """List available GPU nodes.

    Link shows how the GPUs of a node are wired to each other (NV18 = NVLink with
    18 links, PCIe/SYS = PCIe only, worst class shown); Net↓/↑ is a parallel-stream
    probe against a CDN edge in Mbps — what a weight download sees — while
    Upload/Download are the smoothed speed-test figures. Both are "—" until the
    node's validator reports them.
    """
    output_format = resolve_output_format(output_format, json_output)

    _, error = validation.validate(limit, lat, lon, max_distance, min_cuda_version, min_download_mbps)
    if error:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    ctx = {
        "lium": lium,
        "gpu_type": gpu_type,
        "gpu_count": gpu_count,
        "lat": lat,
        "lon": lon,
        "max_distance": max_distance,
        "min_cuda_version": min_cuda_version,
        "nvlink": nvlink,
        "min_download_mbps": min_download_mbps,
    }

    action = GetExecutorsAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading nodes", lambda: action.execute(ctx))

    executors = result.data["executors"]

    # Check if empty
    if not executors:
        if output_format == "json":
            click.echo("[]")
            return
        if nvlink or min_download_mbps is not None:
            wanted = [w for w in (
                "NVLink between every GPU pair" if nvlink else None,
                f"ingress ≥ {min_download_mbps:g} Mbps" if min_download_mbps is not None else None,
            ) if w]
            ui.error(f"No available node reports {' and '.join(wanted)}")
            # each filter's rule, as the help text states it: --nvlink needs a topology report; --min-download
            # judges the CDN probe when there is one, else the Download (Mbps) figure
            rules = [r for r in (
                "--nvlink excludes nodes with no topology report yet" if nvlink else None,
                "--min-download judges the CDN probe, else Download (Mbps)" if min_download_mbps is not None else None,
            ) if r]
            ui.info(f"{'; '.join(rules)}. "
                    f"Drop the filter and check on the pod: {ui.styled('nvidia-smi topo -m', 'success')}")
            return
        if gpu_type:
            ui.error(f"All {gpu_type} GPUs are currently rented out")
            ui.info(f"Tip: {ui.styled('lium ls', 'success')}")
        else:
            ui.error("All GPUs are currently rented out")
            ui.info("Check back later or contact support if this persists")
        return


    if output_format == "json":
        sorted_executors, pareto_flags = display.sort_executors(
            executors, sort_by=sort_by, limit=limit
        )
        payload = [
            display.compact_executor(exe, is_pareto, idx)
            for idx, (exe, is_pareto) in enumerate(zip(sorted_executors, pareto_flags), 1)
        ]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        store_executor_selection(sorted_executors)
        return

    # Build table
    table, sorted_executors, header, tip = display.build_executors_table(
        executors,
        sort_by=sort_by,
        limit=limit,
    )

    # Display
    ui.info(header)
    ui.print(table)
    ui.print("")
    ui.info(tip)

    # Store selection for index-based access in up command
    store_executor_selection(sorted_executors)
