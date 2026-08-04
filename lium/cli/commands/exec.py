"""Execute commands on pods using Lium SDK."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Mapping, Optional, Tuple

import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lium.sdk import Lium, PodInfo
from ..utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    _emit_json_error,
    console,
    handle_errors,
    loading_status,
    parse_targets,
)


@dataclass(frozen=True)
class PodExecution:
    """What one pod printed and how its command ended."""

    pod: str
    stdout: str
    stderr: str
    exit_code: int
    error: str | None

    @classmethod
    def from_sdk_result(cls, pod: PodInfo, result: Mapping[str, object]) -> "PodExecution":
        # The SDK omits exit_code only when it could not run the command at all.
        exit_code = result.get("exit_code", EXIT_GENERAL_ERROR)
        return cls(
            pod=pod.huid,
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            exit_code=int(exit_code) if isinstance(exit_code, int) else EXIT_GENERAL_ERROR,
            error=str(result["error"]) if result.get("error") else None,
        )

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def print_execution_for_a_human(execution: PodExecution, show_pod_header: bool) -> None:
    """Print output the same way whether the command succeeded or not.

    The log a caller needs to diagnose a failure is exactly the log a failing
    command produced, so dropping it on failure blinds the caller at the worst
    moment.
    """
    if show_pod_header:
        console.info(f"\n── {execution.pod} ──")

    if execution.stdout:
        print(execution.stdout, end="")
    if execution.stderr:
        print(f"[{console.theme.get('warning', 'yellow')}]{execution.stderr}[/]", end="")

    if not execution.succeeded:
        if execution.error:
            console.error(f"Error: {execution.error}")
        else:
            console.error(f"Command failed (exit code: {execution.exit_code})")


def resolve_command_to_run(command: Optional[str], script: Optional[str]) -> str:
    """The command text to run remotely, from either COMMAND or --script."""
    if not command and not script:
        console.error("Error: Either COMMAND or --script must be provided")
        raise SystemExit(EXIT_CONFIGURATION_ERROR)

    if command and script:
        console.error("Error: Cannot use both COMMAND and --script")
        raise SystemExit(EXIT_CONFIGURATION_ERROR)

    if command:
        return command

    try:
        return Path(script).read_text()
    except OSError as e:
        console.error(f"Error reading script: {e}")
        raise SystemExit(EXIT_CONFIGURATION_ERROR)


def parse_environment_variables(env: Tuple[str, ...]) -> dict[str, str]:
    """KEY=VALUE pairs from -e, rejecting anything that is not a pair."""
    env_dict: dict[str, str] = {}
    for env_var in env:
        if "=" not in env_var:
            console.error(f"Error: Invalid env format '{env_var}' (use KEY=VALUE)")
            raise SystemExit(EXIT_CONFIGURATION_ERROR)
        key, value = env_var.split("=", 1)
        env_dict[key] = value
    return env_dict


def resolve_pods_or_exit(lium: Lium, targets: str, json_output: bool) -> list[PodInfo]:
    """Pods matching TARGETS. A target that matches nothing is a hard failure."""
    with loading_status("Loading pods", ""):
        all_pods = lium.ps()

    selected_pods = parse_targets(targets, all_pods)
    if selected_pods:
        return selected_pods

    message = f"No pods match targets: {targets}"
    if json_output:
        _emit_json_error("pod_not_found", message, EXIT_POD_NOT_FOUND)
    console.error(message)
    raise SystemExit(EXIT_POD_NOT_FOUND)


def report_executions(executions: list[PodExecution], json_output: bool) -> None:
    """Render every pod's result, then leave the exit code to the caller."""
    if json_output:
        payload = {
            "ok": all(execution.succeeded for execution in executions),
            "results": [asdict(execution) for execution in executions],
        }
        click.echo(json.dumps(payload, sort_keys=True))
        return

    show_pod_header = len(executions) > 1
    for execution in executions:
        print_execution_for_a_human(execution, show_pod_header=show_pod_header)

    if show_pod_header:
        succeeded = sum(1 for execution in executions if execution.succeeded)
        console.dim(f"\nCompleted: {succeeded}/{len(executions)} successful")


@click.command("exec")
@click.argument("targets")
@click.argument("command", required=False)
@click.option("--script", "-s", type=click.Path(exists=True), help="Execute a script file")
@click.option("--env", "-e", multiple=True, help="Set environment variables (KEY=VALUE)")
@click.option(
    "--json", "json_output", is_flag=True,
    help="Print machine-readable JSON (stdout, stderr, exit_code) instead of raw output",
)
@handle_errors
def exec_command(
    targets: str,
    command: Optional[str],
    script: Optional[str],
    env: Tuple[str],
    json_output: bool,
):
    """Execute commands on GPU pods.
    
    \b
    TARGETS: Pod identifiers - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)
      - Comma-separated (1,2,eager-wolf-aa)
      - All pods (all)
    
    COMMAND: Command to execute
    
    \b
    Examples:
      lium exec eager-wolf-aa "nvidia-smi"     # Run on specific pod
      lium exec 1 "python --version"           # Run on pod #1 from ps
      lium exec 1,2,3 "uptime"                 # Run on multiple pods
      lium exec all "df -h"                    # Run on all pods
      lium exec 1 --script setup.sh            # Run script on pod
      lium exec 1 -e API_KEY=xyz "python app.py"  # With env vars
      lium exec 1 --json "python train.py"     # Machine-readable result

    \b
    The process exits with the remote command's exit code, so
    'lium exec <pod> "cmd" && next-step' behaves the way a caller expects.
    """
    command_to_run = resolve_command_to_run(command, script)
    env_dict = parse_environment_variables(env)

    lium = Lium()
    selected_pods = resolve_pods_or_exit(lium, targets, json_output)

    if not json_output:
        if len(selected_pods) == 1:
            console.info(f"Executing on {console.get_styled(selected_pods[0].huid, 'pod_id')}")
        else:
            console.info(f"Executing on {len(selected_pods)} pods")

        if env_dict:
            console.dim(f"Environment: {', '.join(f'{k}={v}' for k, v in env_dict.items())}")

    if len(selected_pods) == 1:
        results = [lium.exec(selected_pods[0], command=command_to_run, env=env_dict)]
    else:
        results = lium.exec_all(selected_pods, command=command_to_run, env=env_dict)

    executions = [
        PodExecution.from_sdk_result(pod, result)
        for pod, result in zip(selected_pods, results)
    ]
    report_executions(executions, json_output)

    # Any pod failing fails the whole invocation, like a shell pipeline would.
    raise SystemExit(max(execution.exit_code for execution in executions))
