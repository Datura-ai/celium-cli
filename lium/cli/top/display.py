"""Rendering for `lium top`."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.table import Table

from lium.sdk import GpuStats, PodInfo


def _num(value: Optional[float], unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{unit}"


def _memory(gpu: GpuStats) -> str:
    if gpu.memory_used_mib is None or gpu.memory_total_mib is None:
        return "-"
    used_gb = gpu.memory_used_mib / 1024
    total_gb = gpu.memory_total_mib / 1024
    pct = f" ({gpu.memory_pct:.0f}%)" if gpu.memory_pct is not None else ""
    return f"{used_gb:.1f}/{total_gb:.1f} GiB{pct}"


def build_table(readings: List[Dict[str, Any]], show_pod: bool) -> Table:
    """One row per GPU. ``readings`` are the dicts :func:`pod_reading` returns."""
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, padding=(0, 1))
    if show_pod:
        table.add_column("Pod", no_wrap=True)
    table.add_column("GPU", justify="right", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Util", justify="right", no_wrap=True)
    table.add_column("Memory", justify="right", no_wrap=True)
    table.add_column("Temp", justify="right", no_wrap=True)
    table.add_column("Power", justify="right", no_wrap=True)

    for reading in readings:
        label = reading["pod"]
        if reading.get("error"):
            row = [str(label)] if show_pod else []
            table.add_row(*row, "-", f"[error]{reading['error']}[/error]", "-", "-", "-", "-")
            continue
        for gpu in reading["gpus"]:
            row = [str(label)] if show_pod else []
            table.add_row(
                *row,
                str(gpu.index),
                gpu.name,
                _num(gpu.utilization_pct, "%"),
                _memory(gpu),
                _num(gpu.temperature_c, "°C"),
                _num(gpu.power_draw_w, " W"),
            )
    return table


def summary_line(readings: List[Dict[str, Any]]) -> str:
    """``3 GPUs on 1 pod, avg util 87%, 2 idle`` for the footer."""
    gpus: List[GpuStats] = [g for r in readings if not r.get("error") for g in r["gpus"]]
    pods = len(readings)
    if not gpus:
        return f"no GPU readings from {pods} pod{'s' if pods != 1 else ''}"
    utils = [g.utilization_pct for g in gpus if g.utilization_pct is not None]
    avg = f"avg util {sum(utils) / len(utils):.0f}%" if utils else "util unknown"
    idle = sum(1 for u in utils if u < 5)
    idle_note = f", {idle} idle" if idle else ""
    return f"{len(gpus)} GPU{'s' if len(gpus) != 1 else ''} on {pods} pod{'s' if pods != 1 else ''}, {avg}{idle_note}"


def pod_reading(pod: PodInfo, gpus: Optional[List[GpuStats]] = None, error: Optional[str] = None) -> Dict[str, Any]:
    """What one sample of one pod looks like, before rendering."""
    return {"pod": pod.name or pod.huid, "huid": pod.huid, "gpus": gpus or [], "error": error}


def to_json(readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The `--format json` payload for one sample."""
    return {
        "ok": all(not r.get("error") for r in readings),
        "sampled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pods": [
            {
                "pod": r["pod"],
                "huid": r["huid"],
                "gpus": [g.to_dict() for g in r["gpus"]],
                "error": r.get("error"),
            }
            for r in readings
        ],
    }
