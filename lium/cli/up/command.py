from typing import Optional, Tuple
import click

from lium.sdk import Lium, PodStartError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SSH_ERROR,
    ensure_config,
    handle_errors,
)
from lium.cli.completion import get_gpu_completions
from . import validation, parsing
from .actions import (
    ResolveExecutorAction,
    ResolveTemplateAction,
    CreateEphemeralTemplateAction,
    CreateVolumeAction,
    RentPodAction,
    WaitReadyAction,
    ScheduleTerminationAction,
    VerifyGpuCountAction,
    InstallJupyterAction,
    PrepareSSHAction,
)


@click.command("up")
@click.argument("executor_id", required=False, metavar="NODE_ID")
@click.option("--name", "-n", help="Custom pod name")
@click.option("--template_id", "-t", help="Template ID")
@click.option("--volume", "-v", help="Volume spec: 'id:<HUID>' or 'new:name=<NAME>[,desc=<DESC>]'")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--gpu", help="Filter nodes by GPU type (e.g., H200, A6000)", shell_complete=get_gpu_completions)
@click.option("--count", "-c", type=int, help="Number of GPUs per pod")
@click.option("--country", help="Filter nodes by ISO country code (e.g., US, FR)")
@click.option("--ports", "-p", type=int, help="Minimum number of available ports required")
@click.option("--ttl", help="Auto-terminate after duration (e.g., 6h, 45m, 2d)")
@click.option("--until", help="Auto-terminate at time in local timezone (e.g., 'today 23:00', 'tomorrow 01:00', '2025-10-20 15:30')")
@click.option("--jupyter", is_flag=True, help="Install Jupyter Notebook (automatically selects available port)")
@click.option("--no-ssh", "no_ssh", is_flag=True, help="Create the pod and return instead of opening an SSH session")
@click.option(
    "--verify-gpus", "verify_gpus", is_flag=True,
    help="After the pod is ready, count the GPUs nvidia-smi sees over SSH and compare with the billed count",
)
@click.option(
    "--strict-gpus", "strict_gpus", is_flag=True,
    help="Remove the pod automatically when its GPU count does not match what was requested or billed",
)
@click.option(
    "--ready-timeout",
    "ready_timeout",
    type=click.IntRange(min=1),
    default=None,
    metavar="SECONDS",
    help="Give up waiting for the pod to become ready after this many seconds (exit 1, pod left running and named). Default: wait until it is ready or fails.",
)
@click.option("--restore-backup", "restore_backup_id", help="Backup ID to restore after the pod starts")
@click.option("--restore-to", "restore_path", help="New or empty subdirectory for the startup restore")
@click.option("--image", help="Docker image to run (e.g., pytorch/pytorch:2.0, nvidia/cuda:12.0)")
@click.option("--internal-ports", help="Internal ports to expose (comma-separated, e.g., 22,8000,8080)")
@click.option("--dockerfile", type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to a Dockerfile to build the pod image from (custom build; mutually exclusive with --image/--template_id)")
@click.option("-e", "--env", multiple=True, help="Environment variables (KEY=VALUE), can be repeated")
@click.option("--entrypoint", default="", help="Container entrypoint")
@click.option("--cmd", default="", help="Command to run in the container")
@click.option("--ssh-name", default=None, help="Name to register a new SSH key under (default: cli-<user>@<hostname>)")
@click.option(
    "--volume-encryption/--no-volume-encryption",
    default=True,
    help="Encrypt the local volume when supported (enabled by default)",
)
@handle_errors
def up_command(
    executor_id: Optional[str],
    name: Optional[str],
    template_id: Optional[str],
    volume: Optional[str],
    yes: bool,
    gpu: Optional[str],
    count: Optional[int],
    country: Optional[str],
    ports: Optional[int],
    ttl: Optional[str],
    until: Optional[str],
    jupyter: bool,
    no_ssh: bool,
    verify_gpus: bool,
    strict_gpus: bool,
    ready_timeout: Optional[int],
    restore_backup_id: Optional[str],
    restore_path: Optional[str],
    image: Optional[str],
    internal_ports: Optional[str],
    dockerfile: Optional[str],
    env: Tuple[str, ...],
    entrypoint: Optional[str],
    cmd: Optional[str],
    ssh_name: Optional[str],
    volume_encryption: bool,
):
    """\b
    Create a new GPU pod on a node.
    \b
    NODE_ID: Node UUID, HUID, or index from last 'lium ls'.
    If not provided, uses filters to auto-select best node.
    \b
    Examples:
      lium up cosmic-hawk-f2                # Create pod on specific node
      lium up 1                             # Create pod on node #1 from last ls
      lium up --gpu H200                    # Auto-select best H200 node
      lium up --gpu A6000 -c 2              # Auto-select best 2×A6000 node
      lium up --country US                  # Auto-select best node in US
      lium up --gpu H200 --country FR       # Combine multiple filters
      lium up --ports 5                     # Auto-select with minimum 5 ports
      lium up 1 --name my-pod               # Create with custom name
      lium up 1 --volume id:brave-fox-3a    # Attach existing volume by HUID
      lium up 1 --volume new:name=my-data   # Create and attach new volume
      lium up 1 --volume new:name=my-data,desc="Training data"  # With description
      lium up 1 --ttl 6h                    # Auto-terminate after 6 hours
      lium up 1 --until "today 23:00"       # Auto-terminate at 23:00 local time today
      lium up 1 --until "tomorrow 01:00"    # Auto-terminate at 01:00 local time tomorrow
      lium up 1 --jupyter                   # Install Jupyter Notebook (auto-selects port)
      lium up --gpu H200 -c 8 --verify-gpus # Fail if the pod exposes fewer GPUs than billed
      lium up --gpu H200 -c 8 --verify-gpus --strict-gpus  # ...and remove the pod on mismatch
      lium up 1 --restore-backup BACKUP_ID --restore-to /root/restored
      LIUM_DEBUG=1 lium up 1 --jupyter      # Show debug information
    \b
    Docker-run style (streams logs instead of SSH):
      lium up --gpu A4000 --image pytorch/pytorch:2.0
      lium up --gpu H100 --image vllm/vllm-openai:latest -e HF_TOKEN=xxx
      lium up --gpu A6000 --image python:3.11 --cmd "python -c 'print(1+1)'"
      lium up --gpu A4000 --image myimg --entrypoint /bin/sh --cmd "-c 'echo hi'"
      lium up --gpu A4000 --image myimg --internal-ports 22,8000,8080
    \b
    Custom Dockerfile build (image built remotely from your Dockerfile):
      lium up --gpu A4000 --dockerfile ./Dockerfile
      lium up cosmic-hawk-f2 --dockerfile ./Dockerfile --name my-build
    """
    ensure_config()

    # Check if we're in docker-run mode or custom-Dockerfile build mode
    docker_run_mode = image is not None
    dockerfile_mode = dockerfile is not None

    valid, error = validation.validate(
        executor_id, gpu, count, country, ttl, until, image, template_id, dockerfile
    )
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)
    if bool(restore_backup_id) != bool(restore_path):
        raise CliFailure(
            "invalid_arguments",
            "--restore-backup and --restore-to must be provided together",
            EXIT_CONFIGURATION_ERROR,
        )

    # Parse env vars if provided
    env_dict = {}
    if env:
        env_dict, error = validation.parse_env_vars(env)
        if error:
            raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    parsed, error = parsing.parse(ttl, until, volume)
    if error:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    termination_time = parsed.get("termination_time")
    volume_id = parsed.get("volume_id")
    volume_create_params = parsed.get("volume_create_params")

    # Custom-Dockerfile build: read the Dockerfile text the CLI will send to the
    # backend (the image is built remotely; no build context is uploaded).
    dockerfile_content = None
    if dockerfile_mode:
        from pathlib import Path

        # These flags only apply to template/--image mode. In a custom build the
        # Dockerfile itself defines the image's env/entrypoint/command/ports, so
        # reject them explicitly rather than silently dropping them.
        unsupported = [
            flag
            for flag, supplied in (
                ("--env", bool(env)),
                ("--entrypoint", bool(entrypoint)),
                ("--cmd", bool(cmd)),
                ("--internal-ports", bool(internal_ports)),
            )
            if supplied
        ]
        if unsupported:
            raise CliFailure(
                "invalid_arguments",
                f"{', '.join(unsupported)} cannot be combined with --dockerfile "
                "(the Dockerfile defines the image's env, entrypoint, command, and ports)",
                EXIT_CONFIGURATION_ERROR,
            )

        try:
            dockerfile_content = Path(dockerfile).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliFailure(
                "unreadable_dockerfile",
                f"Could not read Dockerfile: {exc}",
                EXIT_CONFIGURATION_ERROR,
            )
        if not dockerfile_content.strip():
            raise CliFailure(
                "empty_dockerfile", "Dockerfile is empty", EXIT_CONFIGURATION_ERROR
            )
        max_bytes = 64 * 1024
        size_bytes = len(dockerfile_content.encode("utf-8"))
        if size_bytes > max_bytes:
            raise CliFailure(
                "dockerfile_too_large",
                f"Dockerfile is too large ({size_bytes} bytes); max is {max_bytes} bytes (64 KiB)",
                EXIT_CONFIGURATION_ERROR,
            )

    lium = Lium(source="cli")
    if restore_backup_id:
        restore_backup_id = ui.load(
            "Resolving backup ID", lambda: lium.resolve_backup_id(restore_backup_id)
        )

    action = ResolveExecutorAction()
    result = ui.load(
        "Finding node",
        lambda: action.execute({
            "lium": lium,
            "executor_id": executor_id,
            "gpu": gpu,
            "count": count,
            "country": country,
            "ports": ports
        })
    )

    if not result.ok:
        raise CliFailure("node_selection_failed", result.error, EXIT_GENERAL_ERROR)

    executor = result.data["executor"]

    def _show_estimate(est_secs, dl_speed, img_gb, is_slow, warning_msg):
        est_min, est_sec = divmod(est_secs, 60)
        est_str = f"{est_min}m {est_sec}s" if est_min else f"{est_sec}s"
        img_str = f"image: ~{img_gb:.1f} GB, " if img_gb is not None else ""
        ui.dim(f"Est. deploy time: ~{est_str} ({img_str}download: {int(dl_speed)} Mbps)")
        if is_slow and warning_msg:
            ui.warning(f"Warning: {warning_msg}")

    # Resolve or create template (skipped for custom Dockerfile builds, which are
    # built remotely from the supplied Dockerfile and use no template).
    template = None
    if dockerfile_mode:
        pass
    elif docker_run_mode:
        # Parse internal ports (default to [22] if not specified)
        ports_list = [22]
        if internal_ports:
            try:
                ports_list = [int(p.strip()) for p in internal_ports.split(",")]
                # Ensure port 22 is included for SSH access
                if 22 not in ports_list:
                    ports_list.insert(0, 22)
            except ValueError:
                raise CliFailure(
                    "invalid_ports",
                    "Invalid port format. Use comma-separated integers (e.g., 22,8000,8080)",
                    EXIT_CONFIGURATION_ERROR,
                )

        action = CreateEphemeralTemplateAction()
        result = ui.load(
            "Creating template",
            lambda: action.execute({
                "lium": lium,
                "image": image,
                "env": env_dict,
                "entrypoint": entrypoint,
                "cmd": cmd,
                "ports": ports_list,
            })
        )
        template = result.data["template"]
    else:
        action = ResolveTemplateAction()
        result = action.execute({
            "lium": lium,
            "template_id": template_id,
            "executor": executor
        })
        if not result.ok:
            raise CliFailure("template_failed", result.error, EXIT_GENERAL_ERROR)
        template = result.data["template"]
        # API-based estimate using resolved template ID
        try:
            estimate = lium.get_deployment_estimate(executor.id, template.id)
            est_secs = estimate.get("estimated_seconds")
            if est_secs:
                dl_speed = executor.download_speed
                raw_bytes = estimate.get("docker_image_size")
                img_gb = raw_bytes / 1e9 if raw_bytes is not None else None
                _show_estimate(
                    est_secs, dl_speed, img_gb,
                    estimate.get("is_slow_machine", False),
                    estimate.get("warning_message"),
                )
        except Exception:
            pass

    if not yes:
        confirm_msg = (
            f"Acquire pod on {executor.huid} "
            f"({executor.gpu_count}×{executor.gpu_type}) "
            f"at ${executor.price_per_hour:.2f}/h?"
        )
        if restore_backup_id:
            confirm_msg += f" Restore backup {restore_backup_id} to {restore_path} after startup."
        if not ui.confirm(confirm_msg):
            return

    if volume_create_params:
        action = CreateVolumeAction()
        result = ui.load(
            f"Creating volume '{volume_create_params['name']}'",
            lambda: action.execute({
                "lium": lium,
                "volume_create_params": volume_create_params
            })
        )

        volume_id = result.data["volume_id"]

    action = RentPodAction()
    result = ui.load(
        "Renting machine",
        lambda: action.execute({
            "lium": lium,
            "executor": executor,
            "template": template,
            "dockerfile_content": dockerfile_content,
            "name": name,
            "volume_id": volume_id,
            "ports": ports,
            "ssh_name": ssh_name,
            "enable_volume_encryption": volume_encryption,
            "backup_id": restore_backup_id,
            "restore_path": restore_path,
        })
    )

    pod_id = result.data["pod_id"]
    pod_name = result.data["pod_name"]

    # The pod is rented and already billing from here on. Every failure below
    # names it before propagating, or the caller cannot clean up what it pays for.
    action = WaitReadyAction()
    try:
        result = ui.load(
            "Loading image",
            lambda: action.execute({
                "lium": lium,
                "pod_id": pod_id,
                "timeout": ready_timeout,
            })
        )
    except PodStartError as exc:
        # The pod is dead (FAILED/STOPPED) or gone; say so with its last status so
        # a script does not retry a rent that will never come up.
        label = exc.pod.huid if exc.pod is not None else pod_name
        raise CliFailure(
            "pod_start_failed",
            f"Pod {label} (id: {pod_id}) failed to start: {exc}. "
            f"Check 'lium ps' and remove it with 'lium rm {label}' if it is still listed.",
            EXIT_API_ERROR,
        )
    except Exception:
        ui.error(f"Pod {pod_name} (id: {pod_id}) was created but did not become ready")
        raise

    if not result.ok:
        # Still starting when --ready-timeout ran out: the pod keeps billing, so
        # name it and hand the decision back to the caller.
        raise CliFailure(
            "pod_not_ready",
            f"Pod {pod_name} (id: {pod_id}) is still starting after {ready_timeout}s and is billing. "
            f"Wait with 'lium ps', or remove it with 'lium rm {pod_name}'.",
            EXIT_GENERAL_ERROR,
        )

    pod = result.data["pod"]
    pod_label = f"Pod {ui.styled(pod.huid, 'pod_id')} (name: {pod_name}, id: {pod_id})"

    if termination_time:
        action = ScheduleTerminationAction()
        try:
            ui.load(
                "Scheduling termination",
                lambda: action.execute({
                    "lium": lium,
                    "pod": pod,
                    "termination_time": termination_time
                })
            )
        except Exception:
            ui.info(f"{pod_label} is running but auto-termination was NOT scheduled")
            raise

    # The GPU count is checked after --ttl is scheduled: a mismatched pod that is
    # left running (no --strict-gpus) must still terminate when the caller asked.
    # The requested count is --count, or the chosen node's count when there was none.
    action = VerifyGpuCountAction()
    verify_ctx = {
        "lium": lium,
        "pod": pod,
        "expected_count": count if count is not None else executor.gpu_count,
        "executor_id": executor.id,
        "verify_via_ssh": verify_gpus,
    }
    if verify_gpus:
        result = ui.load("Verifying GPU count", lambda: action.execute(verify_ctx))
    else:
        result = action.execute(verify_ctx)
    if not result.ok:
        ui.error(result.error)
        if strict_gpus and result.data.get("mismatch"):
            ui.load("Removing pod", lambda: lium.rm(pod))
            ui.info(f"{pod_label} removed (--strict-gpus)")
            raise CliFailure(
                "gpu_count_mismatch",
                f"{result.error}; pod removed",
                EXIT_GENERAL_ERROR,
                data=result.data,
            )
        ui.info(f"{pod_label} is running with the GPU count above; remove it with 'lium rm {pod.huid}'")
        raise CliFailure(
            "gpu_count_mismatch" if result.data.get("mismatch") else "gpu_verification_failed",
            result.error,
            EXIT_GENERAL_ERROR,
            data=result.data,
        )

    if jupyter:
        action = InstallJupyterAction()
        try:
            result = ui.load(
                "Installing Jupyter",
                lambda: action.execute({
                    "lium": lium,
                    "pod": pod,
                    "ui": ui
                })
            )
        except Exception:
            ui.info(f"{pod_label} is running but Jupyter was NOT installed")
            raise

        if not result.ok:
            ui.info(pod_label)
            raise CliFailure(
                "jupyter_install_failed",
                f"Pod is running but Jupyter was NOT installed: {result.error}",
                EXIT_GENERAL_ERROR,
            )

    # Always state what was created: a caller that only gets an SSH banner or a
    # log stream has no way to name the pod it is now paying for.
    ui.info(f"{pod_label} ready")

    if restore_backup_id:
        ui.warning(
            f"Restore is continuing in {restore_path}. Do not modify that directory until the restore completes."
        )

    if no_ssh:
        return

    # Docker-run mode: stream logs instead of SSH
    if docker_run_mode:
        from lium.cli.logs.actions import StreamLogsAction

        ui.dim(f"Streaming logs from {pod_name}... (Ctrl+C to stop)")

        ctx = {"lium": lium, "pod": pod, "tail": 100, "follow": True}
        action = StreamLogsAction()

        try:
            for line in action.execute(ctx):
                click.echo(line)
        except KeyboardInterrupt:
            ui.dim("\nStopped following logs")
        return

    # Standard mode: SSH into the pod
    action = PrepareSSHAction()
    result = ui.load(
        "Connecting SSH",
        lambda: action.execute({
            "pod_name": pod_name
        })
    )

    ssh_cmd = result.data["ssh_cmd"]
    pod = result.data["pod"]

    from lium.cli.ssh.command import ssh_session_connected

    if not ssh_session_connected(ssh_cmd):
        raise CliFailure(
            "ssh_connection_failed",
            f"Pod {pod.huid} is running but the SSH connection failed",
            EXIT_SSH_ERROR,
        )
