"""Schema command: the shape of what the CLI and SDK return, as JSON Schema."""

import json
from typing import Tuple

import click

from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, handle_errors
from lium.sdk.schema import ALL_MODELS, PUBLIC_MODELS, schemas


@click.command("schema")
@click.argument("models", nargs=-1, metavar="[MODEL]...")
@click.option("--list", "list_models", is_flag=True, help="List the model names instead of printing schemas")
@click.option("--compact", is_flag=True, help="One line of JSON, no indentation")
@handle_errors
def schema_command(models: Tuple[str, ...], list_models: bool, compact: bool):
    """Print JSON Schema for the SDK's data models.

    Derived from the dataclasses in lium.sdk.models, so it matches what
    `lium ps --format json`, `lium ls --format json`, `lium templates --format json`
    and `PodInfo.to_dict()` produce. Without arguments prints PodInfo,
    ExecutorInfo, Template and GpuStats.

    \b
    Examples:
      lium schema                      # the models command output uses
      lium schema PodInfo              # one model
      lium schema --list               # every model name
      lium schema PodInfo --compact | jq '.PodInfo.properties | keys'
    """
    if list_models:
        for name in ALL_MODELS:
            click.echo(name)
        return

    unknown = [name for name in models if name not in ALL_MODELS]
    if unknown:
        raise CliFailure(
            "unknown_model",
            f"Unknown model(s): {', '.join(unknown)}. Known models: {', '.join(ALL_MODELS)} "
            "('lium schema --list')",
            EXIT_CONFIGURATION_ERROR,
        )

    payload = schemas(list(models) if models else list(PUBLIC_MODELS))
    click.echo(json.dumps(payload, indent=None if compact else 2, sort_keys=False))
