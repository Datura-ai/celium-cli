"""Describe command implementation."""

import json
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import ensure_config, handle_errors
from . import display
from .actions import resolve_pod_or_fail


@click.command("describe")
@click.argument("pod_id")
@click.option("--json", "json_output", is_flag=True, help="Print the manifest as machine-readable JSON")
@handle_errors
def describe_command(pod_id: str, json_output: bool):
    """Show everything known about one pod: ports, GPU, template, billing."""

    # Only the human path may block on the interactive setup. A `--json` caller
    # is a script or an agent behind a pipe: it cannot answer a prompt, so a
    # missing key has to surface as the JSON error envelope Lium() raises.
    if not json_output:
        ensure_config()

    pod = resolve_pod_or_fail(Lium(), pod_id, show_progress=not json_output)
    manifest = display.build_manifest(pod)

    if json_output:
        click.echo(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    ui.print(display.build_manifest_table(manifest))
