"""Describe command implementation."""

import json
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_API_ERROR,
    EXIT_POD_NOT_FOUND,
    ensure_config,
    handle_errors,
)
from . import display
from .actions import GetPodAction


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

    action = GetPodAction()
    ctx = {"lium": Lium(), "target": pod_id}

    if json_output:
        result = action.execute(ctx)
    else:
        result = ui.load("Loading pod", lambda: action.execute(ctx))

    if not result.ok:
        if "not found" in result.error:
            raise CliFailure("pod_not_found", result.error, EXIT_POD_NOT_FOUND)
        raise CliFailure("api_error", result.error, EXIT_API_ERROR)

    manifest = display.build_manifest(result.data["pod"])

    if json_output:
        click.echo(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    ui.print(display.build_manifest_table(manifest))
