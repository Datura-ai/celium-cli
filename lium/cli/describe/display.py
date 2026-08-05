"""Manifest assembly and rendering for the describe command."""

from datetime import datetime, timezone
from typing import Optional
from rich.table import Table

from lium.sdk import PodInfo
from lium.cli.utils import console
# Reused rather than reimplemented: the same timestamp handling and cost rounding
# `lium ps` already applies, so describe and ps never disagree on spend.
from lium.cli.ps.display import _parse_timestamp, _spent_usd

SSH_INTERNAL_PORT = "22"


def _uptime_hours(created_at: str) -> Optional[float]:
    """Hours since pod creation; None when the timestamp is unusable."""
    dt_created = _parse_timestamp(created_at) if created_at else None
    if not dt_created:
        return None
    return round((datetime.now(timezone.utc) - dt_created).total_seconds() / 3600, 2)


def _ports_section(ports: dict) -> dict:
    """Port mapping split into the SSH port and the ports left for services.

    Keys of the mapping are ports *inside* the container, values are the ports
    reachable from outside. Getting that direction wrong is the single most
    common way an agent loses time on a pod, so the manifest states it.
    """
    mapping = ports or {}
    service_ports = [
        {"internal": int(internal), "external": external}
        for internal, external in mapping.items()
        if str(internal) != SSH_INTERNAL_PORT
    ]
    return {
        "mapping": mapping,
        "direction": "internal -> external",
        "ssh_external": mapping.get(SSH_INTERNAL_PORT),
        "service_ports": service_ports,
    }


def build_manifest(pod: PodInfo) -> dict:
    """Everything an agent needs to work on this pod, as one JSON document."""
    executor = pod.executor
    template = pod.template or {}
    price_per_hour = executor.price_per_hour if executor else None

    return {
        "pod": {
            "id": pod.id,
            "huid": pod.huid,
            "name": pod.name,
            "status": pod.status.upper() if pod.status else None,
            "created_at": pod.created_at,
            "uptime_hours": _uptime_hours(pod.created_at),
        },
        "gpu": {
            "type": executor.gpu_type,
            "count": executor.gpu_count,
            "model": executor.gpu_model or None,
            "driver_version": executor.driver_version or None,
            "max_cuda_version": executor.max_cuda_version,
        } if executor else None,
        "machine": {
            "executor_id": executor.id,
            "ip": executor.ip,
            "location": executor.location,
            "tier": executor.tier,
            "docker_in_docker": executor.docker_in_docker,
        } if executor else None,
        "ports": _ports_section(pod.ports),
        "access": {
            "ssh_command": pod.ssh_cmd,
            "jupyter_url": pod.jupyter_url,
        },
        "template": {
            "name": template.get("name") or template.get("template_name"),
            "docker_image": template.get("docker_image"),
            "docker_image_tag": template.get("docker_image_tag"),
        } if template else None,
        "storage": {
            "volume_encryption_enabled": pod.enable_volume_encryption,
            "volume_encryption_status": pod.volume_encryption_status,
        },
        "billing": {
            "price_per_hour": price_per_hour,
            "spent_usd": _spent_usd(pod.created_at, price_per_hour),
            "removal_scheduled_at": pod.removal_scheduled_at,
        },
    }


def _format_ports(ports_section: dict) -> str:
    """One line per mapped port, SSH first."""
    mapping = ports_section["mapping"]
    if not mapping:
        return "—"

    ssh_external = ports_section["ssh_external"]
    lines = []
    if ssh_external:
        lines.append(f"{ssh_external} → 22 (ssh)")
    lines.extend(
        f"{service['external']} → {service['internal']}"
        for service in ports_section["service_ports"]
    )
    return "\n".join(lines)


def build_manifest_table(manifest: dict) -> Table:
    """Human-readable rendering of the same manifest the --json flag emits."""
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column(justify="left", style="dim", no_wrap=True)
    table.add_column(justify="left", overflow="fold")

    pod = manifest["pod"]
    gpu = manifest["gpu"]
    machine = manifest["machine"]
    template = manifest["template"]
    billing = manifest["billing"]

    table.add_row("Pod", console.get_styled(pod["huid"] or "—", "pod_id"))
    table.add_row("Name", pod["name"] or "—")
    table.add_row("Status", f"[{console.pod_status_color(pod['status'] or '')}]{pod['status'] or '—'}[/]")
    table.add_row("Uptime", f"{pod['uptime_hours']}h" if pod["uptime_hours"] is not None else "—")

    if gpu:
        config = f"{gpu['count']}×{gpu['type']}" if (gpu["count"] or 0) > 1 else (gpu["type"] or "—")
        table.add_row("GPU", config)
        table.add_row("Driver", gpu["driver_version"] or "—")
        table.add_row("Max CUDA", str(gpu["max_cuda_version"]) if gpu["max_cuda_version"] else "—")
    if machine:
        table.add_row("IP", machine["ip"] or "—")
        table.add_row("Tier", machine["tier"] or "—")
    if template:
        table.add_row("Template", template["name"] or "—")
        table.add_row("Image", template["docker_image"] or "—")

    table.add_row("Ports", _format_ports(manifest["ports"]))
    table.add_row("SSH", manifest["access"]["ssh_command"] or "—")

    price = billing["price_per_hour"]
    spent = billing["spent_usd"]
    table.add_row("$/h", f"${price:.2f}" if price is not None else "—")
    table.add_row("Spent", f"${spent:.2f}" if spent is not None else "—")
    if billing["removal_scheduled_at"]:
        table.add_row("Removal at", billing["removal_scheduled_at"])

    return table
