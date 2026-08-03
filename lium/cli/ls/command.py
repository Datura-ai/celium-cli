"""List (ls) command implementation."""

import json
from typing import Optional, List, Tuple
import click

from lium.sdk import Lium, ExecutorInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    calculate_pareto_frontier,
    handle_errors,
    store_executor_selection,
)
from lium.cli.completion import get_gpu_completions
from . import validation, display
from .actions import GetExecutorsAction


def store_sorted_executors(executors: List[ExecutorInfo]) -> List[ExecutorInfo]:
    """Pareto-sort *executors* and remember them for index-based access in ``lium up``."""
    pareto_flags = calculate_pareto_frontier(executors)
    executors_with_pareto = list(zip(executors, pareto_flags))

    executors_with_pareto = sorted(
        executors_with_pareto,
        key=lambda x: (not x[1], -x[0].download_speed)
    )

    sorted_executors = [e for e, _ in executors_with_pareto]
    store_executor_selection(sorted_executors)

    return sorted_executors


@click.command("ls")
@click.option("--gpu", "gpu_type", shell_complete=get_gpu_completions, help="Filter by GPU type, e.g. A100")
@click.option("--count", "gpu_count", type=int, help="Renter-intent GPU count (widens to splittable nodes where min <= N <= available)")
@click.option("--min-cuda", "min_cuda_version", type=float, help="Minimum CUDA version, e.g. 12.4 (NVIDIA drivers are backward compatible)")
@click.option("--tier", type=click.Choice(["any", "secure", "spot"]), default=None, help="Only nodes of this tier (spot nodes are cheaper but reclaimable)")
@click.option("--min-reliability", type=float, help="Minimum provider reliability score, 0-100")
@click.option("--scored-only", is_flag=True, help="With --min-reliability, also drop nodes that have no score yet")
@click.option("--min-uptime", "min_uptime_days", type=float, help="Minimum current uptime, in days")
@click.option("--min-vram", "min_vram_gb", type=float, help="Minimum VRAM per GPU, in GB")
@click.option("--min-vram-total", "min_vram_total_gb", type=float, help="Minimum VRAM across the whole node, in GB")
@click.option("--max-price", "max_price_total", type=float, help="Maximum total $/hour (prices the split --count would rent)")
@click.option("--max-price-gpu", "max_price_per_gpu", type=float, help="Maximum $/GPU-hour, matching the $/GPU·h column")
@click.option("--country", "countries", multiple=True, help="ISO country code, e.g. US. Repeat for several countries")
@click.option("--ports", "min_ports", type=int, help="Minimum number of open ports")
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
    min_cuda_version: Optional[float],
    tier: Optional[str],
    min_reliability: Optional[float],
    scored_only: bool,
    min_uptime_days: Optional[float],
    min_vram_gb: Optional[float],
    min_vram_total_gb: Optional[float],
    max_price_total: Optional[float],
    max_price_per_gpu: Optional[float],
    countries: Tuple[str, ...],
    min_ports: Optional[int],
):
    """\b
    List available GPU nodes.
    \b
    Hardware filters:
      --gpu, --count, --min-vram, --min-vram-total, --min-cuda, --ports
    \b
    Price filters:
      --max-price       total $/hour, the number you pay
      --max-price-gpu   $/GPU-hour, the $/GPU·h column
    \b
    Trust filters:
      --tier, --min-reliability, --scored-only, --min-uptime
    \b
    Location filters:
      --country, --lat/--lon, --max-distance
    """

    _, error = validation.validate(limit, lat, lon, max_distance, min_cuda_version)
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
        "tier": tier,
        "min_reliability": min_reliability,
        "include_unscored": not scored_only,
        "min_uptime_minutes": int(min_uptime_days * 1440) if min_uptime_days else None,
        "min_vram_gb": min_vram_gb,
        "min_vram_total_gb": min_vram_total_gb,
        "min_ports": min_ports,
        "countries": list(countries) or None,
        "max_price_total": max_price_total,
        "max_price_per_gpu": max_price_per_gpu,
    }

    action = GetExecutorsAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading nodes", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    executors = result.data["executors"]

    # Check if empty
    if not executors:
        if output_format == "json":
            click.echo("[]")
            return
        if gpu_type:
            ui.error(f"All {gpu_type} GPUs are currently rented out")
            ui.info(f"Tip: {ui.styled('lium ls', 'success')}")
        else:
            ui.error("No nodes match those filters")
            ui.info("Check back later or loosen the filters")
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
