"""Higher-level decorators built on top of the Lium SDK."""

import inspect
import json
import os
import random
import re
import shlex
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import List, Optional, Sequence, Tuple

from .client import Lium
from .exceptions import LiumError
from .models import ExecutorInfo

# How long the pod may outlive the call before the server removes it on its own.
# It covers the boot wait, the result download and a caller that dies mid-call.
_TTL_MARGIN = timedelta(minutes=15)
# TTL when the call itself has no timeout: still a bound, not "forever".
_TTL_NO_TIMEOUT = timedelta(hours=24)
_BOOT_TIMEOUT = 300

_SPEC_RE = re.compile(r"^\s*(?:(\d+)\s*[x×]\s*)?(.+?)\s*$", re.IGNORECASE)


def _parse_machine(spec: str) -> Tuple[int, str]:
    """``"1xH200"`` → ``(1, "H200")``; ``"RTX 4090"`` → ``(1, "RTX4090")``; ``"2xA100"`` → ``(2, "A100")``.

    The count defaults to 1: a function offloaded to "an H200" wants one H200, not the
    first eight-GPU node the API happens to list.
    """
    m = _SPEC_RE.match(spec or "")
    if not m or not m.group(2):
        raise ValueError(f"Invalid machine spec {spec!r}; use e.g. '1xH200', 'RTX4090', '2xA100'")
    return int(m.group(1) or 1), m.group(2).replace(" ", "").upper()


def _select_executor(executors: List[ExecutorInfo], spec: str) -> ExecutorInfo:
    """Cheapest node with exactly ``count`` GPUs of the requested type."""
    count, gpu = _parse_machine(spec)
    matches = [
        e for e in executors
        if e.gpu_count == count
        and (
            e.gpu_type.replace(" ", "").upper() == gpu
            or gpu in e.machine_name.replace(" ", "").upper()
        )
    ]
    if not matches:
        same_type = sorted(
            {f"{e.gpu_count}x{e.gpu_type} ${e.price_per_hour:.2f}/h"
             for e in executors if gpu in e.machine_name.replace(" ", "").upper()}
        )
        hint = f" Available: {', '.join(same_type)}." if same_type else ""
        raise LiumError(f"No node found matching machine type: {spec}.{hint}")
    return min(matches, key=lambda e: e.price_per_hour)


def _say(quiet: bool, func_name: str, msg: str) -> None:
    if not quiet:
        print(f"[lium] {func_name}: {msg}", file=sys.stderr, flush=True)


def machine(
    machine: str,
    template_id: Optional[str] = None,
    cleanup: bool = True,
    requirements: Optional[Sequence[str]] = None,
    *,
    timeout: Optional[float] = 3600,
    quiet: bool = False,
):
    """Decorator to execute a function on a remote Lium machine.

    Creates a new pod, sends function source code and executes it remotely,
    returns the result, and optionally cleans up the pod.

    Args:
        machine: ``"<count>x<gpu>"`` or ``"<gpu>"`` — e.g. ``"1xH200"``, ``"RTX4090"``,
            ``"2xA100"``. The count defaults to 1. The cheapest available node with
            exactly that many GPUs of that type is rented.
        template_id: Docker template ID (optional, uses the node's default if not specified)
        cleanup: Whether to delete the pod after execution (default: True)
        requirements: Optional iterable of pip-installable packages to install on the pod
        timeout: Seconds the function may run on the pod before it is killed (default 1 h;
            ``None`` for no limit). The pod is also scheduled for removal at
            ``timeout + 15 min`` (24 h when ``timeout=None``) so a caller that dies
            mid-call cannot leave it billing.
        quiet: Suppress the one-line progress messages written to stderr.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Initialize SDK
            sdk = Lium()
            pod_info = None
            started = time.time()
            say = lambda msg: _say(quiet, func.__name__, msg)  # noqa: E731

            try:
                # Step 1: Pick the cheapest node matching "<count>x<gpu>"
                executor = _select_executor(sdk.ls(), machine)

                # Step 2: Create pod
                pod_name = f"remote-{func.__name__}-{int(time.time())}"
                ttl = (timedelta(seconds=timeout) + _TTL_MARGIN) if timeout else _TTL_NO_TIMEOUT
                say(
                    f"renting {executor.gpu_count}x{executor.gpu_type} ${executor.price_per_hour:.2f}/h "
                    f"({executor.huid}, {(executor.location or {}).get('country', '?')}), "
                    f"removal in {ttl.total_seconds() / 3600:.1f}h"
                )

                pod_dict = sdk.up(
                    executor_id=executor.id,
                    name=pod_name,
                    template_id=template_id,
                )
                pod_info = pod_dict  # enough for cleanup (dict with id) until wait_ready returns

                # Server-side safety net: the pod goes away even if this process does not.
                removal_at = datetime.now(timezone.utc) + ttl
                try:
                    sdk.schedule_termination(_pod_ref(pod_dict), termination_time=removal_at.isoformat())
                except Exception as exc:  # noqa: BLE001 — a missing TTL must not fail the call
                    say(f"warning: could not schedule pod removal ({exc}); remove it yourself if this process dies")

                # Wait for pod to be ready
                pod_info = sdk.wait_ready(pod_dict, timeout=_BOOT_TIMEOUT)
                if not pod_info:
                    pod_info = _pod_ref(pod_dict)
                    raise LiumError(f"Pod {pod_name} failed to start within {_BOOT_TIMEOUT}s")
                say(f"pod ready in {time.time() - started:.0f}s")

                # Step 3: Extract function source code without decorators
                func_source = inspect.getsource(func)
                func_name = func.__name__

                # Strip decorator lines - find the 'def' line and keep from there
                lines = func_source.split('\n')
                def_index = next(i for i, line in enumerate(lines) if 'def ' in line)
                func_source = '\n'.join(lines[def_index:])

                # Step 4: Create runner script with function source and arguments
                runner_script = f'''#!/usr/bin/env python3
import sys
import traceback
import json

# Function source code
{func_source}

try:
    # Arguments
    args = {repr(args)}
    kwargs = {repr(kwargs)}

    # Execute function
    result = {func_name}(*args, **kwargs)

    # Save result as JSON
    with open('/tmp/result.json', 'w') as f:
        json.dump({{'success': True, 'result': result}}, f)

except Exception as e:
    # Save error
    with open('/tmp/result.json', 'w') as f:
        json.dump({{
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }}, f)
    sys.exit(1)
'''

                # Write runner script to temp file
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                    runner_file = f.name
                    f.write(runner_script)

                try:
                    # Step 5: Upload runner script
                    sdk.upload(pod_info, local=runner_file, remote='/tmp/runner.py')

                    # Step 6: Create isolated virtual environment
                    venv_path = f"/tmp/lium_venv_{int(time.time())}_{random.randint(1000,9999)}"
                    venv_python = f"{venv_path}/bin/python"
                    venv_cmd = f"python3 -m venv {shlex.quote(venv_path)}"
                    venv_result = sdk.exec(pod_info, command=venv_cmd)
                    if not venv_result['success']:
                        raise LiumError(f"Failed to create virtual environment:\n{venv_result['stderr']}")

                    # Step 7: Install requirements if requested
                    reqs = [req for req in (requirements or []) if req]
                    if reqs:
                        say(f"installing {len(reqs)} package(s): {', '.join(reqs)}")
                        packages = " ".join(shlex.quote(req) for req in reqs)
                        install_cmd = f"{shlex.quote(venv_python)} -m pip install {packages}"
                        install_result = sdk.exec(pod_info, command=install_cmd)
                        if not install_result['success']:
                            raise LiumError(
                                "Failed installing requirements "
                                f"({', '.join(reqs)}):\n{install_result['stderr']}"
                            )

                    # Step 8: Execute runner via virtual environment python, bounded by `timeout`
                    say("running")
                    run_cmd = f"{shlex.quote(venv_python)} /tmp/runner.py"
                    if timeout:
                        # TERM first, KILL 5 s later. (`-s KILL` would kill the process group,
                        # `timeout` included, and the ssh session would report no exit status.)
                        run_cmd = f"timeout -k 5 {int(timeout)} {run_cmd}"
                    exec_result = sdk.exec(pod_info, command=run_cmd)

                    # Step 9: Download result (even when execution failed to capture error details)
                    result_data = None
                    result_file = None
                    try:
                        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
                            result_file = f.name
                        sdk.download(pod_info, remote='/tmp/result.json', local=result_file)
                        with open(result_file, 'r') as f:
                            result_data = json.load(f)
                    except Exception:
                        result_data = None
                    finally:
                        if result_file and os.path.exists(result_file):
                            os.unlink(result_file)

                    if result_data and result_data.get('success'):
                        elapsed = time.time() - started
                        say(f"done in {elapsed:.0f}s (~${executor.price_per_hour * elapsed / 3600:.4f})")
                        return result_data['result']

                    # Construct detailed error message
                    if result_data and not result_data.get('success', True):
                        err_msg = result_data.get('error', 'Unknown remote error')
                        tb = result_data.get('traceback')
                        if tb:
                            err_msg = f"{err_msg}\n\nTraceback:\n{tb}"
                        raise LiumError(f"Remote execution failed:\n{err_msg}")

                    if timeout and exec_result.get('exit_code') == 124:  # coreutils timeout
                        raise LiumError(f"Remote execution of {func_name} exceeded timeout={timeout}s and was killed")

                    stderr = exec_result.get('stderr') or exec_result.get('stdout') or 'Unknown remote error'
                    raise LiumError(f"Remote execution failed:\n{stderr}")

                finally:
                    # Clean up local temp file
                    os.unlink(runner_file)

            finally:
                # Remove virtual environment directory best-effort when pod stays alive
                if pod_info and 'venv_path' in locals() and not cleanup:
                    try:
                        sdk.exec(pod_info, command=f"rm -rf {shlex.quote(venv_path)}")
                    except Exception:
                        pass

                # Step 10: Cleanup pod
                if cleanup and pod_info:
                    try:
                        sdk.down(_pod_ref(pod_info))
                        say("pod removed")
                    except Exception:
                        say("warning: could not remove the pod; it is scheduled for removal server-side")

        return wrapper

    return decorator


class _PodRef:
    """The one attribute ``down``/``schedule_termination`` need before ``wait_ready`` returns a PodInfo."""

    def __init__(self, pod_id: str):
        self.id = pod_id


def _pod_ref(pod):
    if isinstance(pod, dict):
        return _PodRef(pod["id"])
    return pod


__all__ = ["machine"]
