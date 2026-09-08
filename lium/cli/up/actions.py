from typing import Optional, Dict, List
import time

from lium.cli.actions import ActionResult
from lium.sdk import ExecutorInfo, Template, PodInfo, Lium
from lium.cli.utils import (
    calculate_pareto_frontier,
    resolve_executor_indices,
    get_pytorch_template_id,
    wait_ready_no_timeout,
)


class ResolveExecutorAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor_id: Optional[str] = ctx.get("executor_id")
        gpu: Optional[str] = ctx.get("gpu")
        count: Optional[int] = ctx.get("count")
        country: Optional[str] = ctx.get("country")
        ports: Optional[int] = ctx.get("ports")

        if executor_id:
            if executor_id.isdigit():
                resolved_ids, error = resolve_executor_indices([executor_id])
                if error or not resolved_ids:
                    return ActionResult(ok=False, data={}, error=error or "Failed to resolve node index")
                executor_id = resolved_ids[0]

            executor = lium.get_executor(executor_id)
            if not executor:
                return ActionResult(ok=False, data={}, error=f"Node '{executor_id}' not found")

            if ports and (not executor.available_port_count or executor.available_port_count < ports):
                available = executor.available_port_count or 0
                return ActionResult(
                    ok=False,
                    data={},
                    error=f"Node {executor.huid} has insufficient ports (available: {available}, required: {ports})"
                )
        else:
            executors = lium.ls(gpu_type=gpu)

            if count:
                executors = [e for e in executors if e.gpu_count == count]
            if country:
                executors = [
                    e for e in executors
                    if e.location and e.location.get('country_code', '').upper() == country.upper()
                ]
            if ports:
                executors = [
                    e for e in executors
                    if e.available_port_count and e.available_port_count >= ports
                ]

            if not executors:
                filters = []
                if gpu:
                    filters.append(f"GPU type={gpu}")
                if count:
                    filters.append(f"GPU count={count}")
                if country:
                    filters.append(f"country={country}")
                if ports:
                    filters.append(f"min ports={ports}")
                filter_desc = ', '.join(filters) if filters else "specified filters"
                return ActionResult(ok=False, data={}, error=f"No nodes available with {filter_desc}")

            from lium.cli.ls.command import ls_store_executor
            ls_store_executor(gpu_type=gpu)

            pareto_flags = calculate_pareto_frontier(executors)
            pareto_executors = [e for e, is_pareto in zip(executors, pareto_flags) if is_pareto]
            executor = pareto_executors[0] if pareto_executors else executors[0]

        return ActionResult(ok=True, data={"executor": executor})


class ResolveTemplateAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        template_id: Optional[str] = ctx.get("template_id")
        executor: Optional[ExecutorInfo] = ctx.get("executor")

        if template_id:
            template = lium.get_template(template_id)
            if not template:
                return ActionResult(ok=False, data={}, error=f"Template '{template_id}' not found")
        else:
            template = lium.default_docker_template(executor.id) if executor else None
            if not template:
                template = lium.get_template(get_pytorch_template_id())

        return ActionResult(ok=True, data={"template": template})


class CreateEphemeralTemplateAction:
    """Create an ephemeral template for docker-run style execution."""

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        image: str = ctx["image"]
        env: Dict[str, str] = ctx.get("env", {})
        entrypoint: Optional[str] = ctx.get("entrypoint", "")
        cmd: Optional[str] = ctx.get("cmd", "")
        ports: List[int] = ctx.get("ports", [22])

        # Parse image:tag
        if ":" in image:
            docker_image, docker_tag = image.rsplit(":", 1)
        else:
            docker_image = image
            docker_tag = "latest"

        # Generate a unique name for the ephemeral template
        import hashlib
        hash_input = f"{image}{env}{entrypoint}{cmd}"
        short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]
        template_name = f"ephemeral-{short_hash}"

        template = lium.create_template(
            name=template_name,
            docker_image=docker_image,
            docker_image_tag=docker_tag,
            ports=ports,
            start_command=cmd,
            entrypoint=entrypoint,
            environment=env or {},
            is_private=True,
            one_time_template=True,
        )

        return ActionResult(ok=True, data={"template": template})


class CreateVolumeAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        volume_create_params: Dict[str, str] = ctx["volume_create_params"]

        volume = lium.volume_create(
            name=volume_create_params['name'],
            description=volume_create_params.get('description', '')
        )
        return ActionResult(ok=True, data={"volume": volume, "volume_id": volume.id})


class RentPodAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor: ExecutorInfo = ctx["executor"]
        template: Optional[Template] = ctx.get("template")
        dockerfile_content: Optional[str] = ctx.get("dockerfile_content")
        name: Optional[str] = ctx.get("name")
        volume_id: Optional[str] = ctx.get("volume_id")
        ports: Optional[int] = ctx.get("ports")
        ssh_name: Optional[str] = ctx.get("ssh_name")
        enable_volume_encryption: bool | None = ctx.get("enable_volume_encryption")
        backup_id: Optional[str] = ctx.get("backup_id")
        restore_path: Optional[str] = ctx.get("restore_path")

        if not name:
            name = executor.huid

        pod_info = lium.up(
            executor_id=executor.id,
            name=name,
            template_id=template.id if template else None,
            dockerfile_content=dockerfile_content,
            volume_id=volume_id,
            ports=ports,
            ssh_name=ssh_name,
            enable_volume_encryption=enable_volume_encryption,
            backup_id=backup_id,
            restore_path=restore_path,
        )

        pod_id = pod_info.get('id') or pod_info.get('name', '')
        return ActionResult(ok=True, data={"pod_info": pod_info, "pod_id": pod_id, "pod_name": name})


class WaitReadyAction:
    """Wait for the rented pod. ``ctx["timeout"]`` (seconds) bounds the wait; None is unbounded.

    Propagates ``PodStartError`` from the SDK: a pod that FAILED or vanished is
    not a pod worth waiting for, and the caller must be told which pod it is
    still paying for. A timeout is reported as ``ok=False`` with the same intent.
    """

    # A line every this many seconds while nothing changes; every status change prints one too.
    PROGRESS_EVERY_SECONDS = 30

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod_id: str = ctx["pod_id"]
        timeout: Optional[int] = ctx.get("timeout")
        report = ctx.get("report")

        last_seen: dict = {"pod": None}
        pod = wait_ready_no_timeout(
            lium, pod_id, timeout=timeout, on_poll=self._progress(report, last_seen) if report else None
        )
        if pod is None:
            error = f"Pod {pod_id} was still starting after {timeout}s"
            # DAH-3005: the backend's own estimate tells a slow-but-coming pod from a stuck one.
            # It travels in ``data`` too, so the command can put it in its own message.
            hint = last_seen["pod"].eta_hint() if last_seen["pod"] is not None else None
            if hint:
                error += f" (backend: {hint})"
            return ActionResult(ok=False, data={"eta_hint": hint}, error=error)
        return ActionResult(ok=True, data={"pod": pod})

    def _progress(self, report, last_seen: Optional[dict] = None):
        """An on_poll callback that says what the pod is doing, without repeating itself every poll.

        A silent wait is what turned a slow rent into a killed command: nothing tells the caller
        (or an agent behind a pipe, where the spinner is not drawn) whether the pod is PENDING,
        pulling an image, or already gone. DAH-3005: the line carries the backend's estimate and
        creation phase when it sends them, and is printed again whenever the phase moves.
        """
        if report is None:
            return None
        last = {"status": None, "phase": None, "at": 0.0}

        def on_poll(pod: Optional[PodInfo], status: str, elapsed: float) -> None:
            if last_seen is not None and pod is not None:
                last_seen["pod"] = pod
            phase = pod.phase if pod is not None else None
            changed = status != last["status"] or phase != last["phase"]
            if not changed and elapsed - last["at"] < self.PROGRESS_EVERY_SECONDS:
                return
            last["status"], last["phase"], last["at"] = status, phase, elapsed
            label = pod.huid if pod is not None else "pod"
            line = f"waiting for {label}… {status} ({int(elapsed)} s)"
            hint = pod.eta_hint() if pod is not None else None
            if hint:
                line += f" · {hint}"
            report(line)

        return on_poll


class ScheduleTerminationAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        termination_time = ctx["termination_time"]

        termination_time_str = termination_time.isoformat()
        lium.schedule_termination(pod, termination_time=termination_time_str)

        from datetime import datetime, timezone
        time_delta = termination_time - datetime.now(timezone.utc)
        hours_until = time_delta.total_seconds() / 3600

        return ActionResult(
            ok=True,
            data={
                "termination_time": termination_time,
                "hours_until": hours_until
            }
        )


class InstallJupyterAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        ui = ctx.get("ui")

        if not pod.ports:
            return ActionResult(ok=False, data={}, error="No ports allocated to pod for Jupyter installation")

        available_ports = [int(port) for port in pod.ports.keys() if int(port) != 22]

        if not available_ports:
            return ActionResult(
                ok=False,
                data={},
                error="No suitable ports available for Jupyter (only SSH port 22 found)"
            )

        jupyter_port = available_ports[0]

        lium.install_jupyter(pod, jupyter_internal_port=jupyter_port)

        max_wait = 120
        wait_interval = 3
        elapsed = 0
        pod_id = pod.id

        while elapsed < max_wait:
            time.sleep(wait_interval)
            elapsed += wait_interval

            all_pods = lium.ps()
            updated_pod = next((p for p in all_pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)

            if updated_pod and hasattr(updated_pod, 'jupyter_installation_status'):
                if updated_pod.jupyter_installation_status == "SUCCESS":
                    jupyter_url = getattr(updated_pod, 'jupyter_url', None)
                    return ActionResult(
                        ok=True,
                        data={"jupyter_url": jupyter_url, "jupyter_port": jupyter_port}
                    )
                elif updated_pod.jupyter_installation_status == "FAILED":
                    error_details = getattr(updated_pod, 'jupyter_error', '')
                    error_msg = f"Jupyter installation failed"
                    if error_details:
                        error_msg += f": {error_details}"
                    return ActionResult(ok=False, data={}, error=error_msg)

        return ActionResult(
            ok=False,
            data={},
            error="Jupyter installation timed out. Run 'lium ps' to check status"
        )


class PrepareSSHAction:

    def execute(self, ctx: dict) -> ActionResult:
        pod_name: str = ctx["pod_name"]

        from lium.cli.ssh.command import get_ssh_method_and_pod
        ssh_cmd, pod = get_ssh_method_and_pod(pod_name)
        return ActionResult(ok=True, data={"ssh_cmd": ssh_cmd, "pod": pod})
