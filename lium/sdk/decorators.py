"""Higher-level decorators built on top of the Lium SDK."""

import ast
import atexit
import base64
import hashlib
import inspect
import os
import pickle
import re
import shlex
import sys
import tempfile
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .client import Lium
from .exceptions import LiumError, RemoteExecutionError
from .models import ExecutorInfo

# How long the pod may outlive the call before the server removes it on its own.
# It covers the boot wait, the result download and a caller that dies mid-call.
_TTL_MARGIN = timedelta(minutes=15)
# TTL when the call itself has no timeout: still a bound, not "forever".
_TTL_NO_TIMEOUT = timedelta(hours=24)
_BOOT_TIMEOUT = 300
# A warm pod is removed server-side this long after its `keep_warm` window, should the
# caller never come back to remove it.
_WARM_MARGIN = timedelta(minutes=2)

# Pods kept alive between calls, by `_warm_key`. Shared by every decorated function in
# the process so two functions with the same machine spec share one pod.
_WARM: Dict[str, "_Warm"] = {}

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


# --- what travels to the pod -------------------------------------------------------------------

def _function_source(func) -> str:
    """The function's ``def`` alone: decorators and annotations stripped, dedented.

    Decorators would re-run ``@lium.machine`` on the pod; annotations are evaluated
    at definition time and usually name things (``np.ndarray``) the pod does not have.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError) as exc:
        raise LiumError(f"Cannot read the source of {func.__name__}: {exc}") from exc
    node = _def_node(func, source)
    node.decorator_list = []
    node.returns = None
    for arg in ast.walk(node.args):
        if isinstance(arg, ast.arg):
            arg.annotation = None
    return ast.unparse(node)


def _def_node(func, source: str):
    tree = ast.parse(source)
    node = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if node is None:
        raise LiumError(f"{func.__name__} must be defined with `def` (lambdas cannot be sent to a pod)")
    return node


def _code_names(code) -> Set[str]:
    """Names a code object (and its nested functions) looks up outside its locals."""
    names = set(code.co_names) - set(code.co_varnames) - set(code.co_cellvars)
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            names |= _code_names(const)
    return names


def _imported_modules(node) -> Set[str]:
    """Top-level module names the function imports itself (those exist on the pod)."""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            found |= {alias.name.split(".")[0] for alias in sub.names}
        elif isinstance(sub, ast.ImportFrom) and sub.module:
            found.add(sub.module.split(".")[0])
    return found


def _check_portable(func) -> None:
    """Refuse, before renting anything, a function the pod could only fail on.

    Only the function's own source travels: a closure variable or a module-level
    name (constant, import, helper) is a ``NameError`` on the pod, sixty seconds and
    a few cents later.
    """
    code = func.__code__
    free = set(code.co_freevars) - {func.__name__}  # a nested function recursing into itself is fine
    if free:
        raise LiumError(
            f"{func.__name__} closes over {sorted(free)}; only the function's own source "
            "runs on the pod, so pass them as arguments instead"
        )
    try:
        node = _def_node(func, textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError):
        return  # _function_source reports unreadable source
    module_names = set(func.__globals__) - {"__builtins__", func.__name__} - _imported_modules(node)
    used = _code_names(code) & module_names
    if used:
        raise LiumError(
            f"{func.__name__} uses module-level names {sorted(used)}, which do not exist on the pod: "
            "import inside the function or pass them as arguments"
        )


def _runner_script(source: str, func_name: str, is_async: bool, args, kwargs, result_path: str) -> str:
    try:
        blob = base64.b64encode(pickle.dumps((args, kwargs), protocol=4)).decode()
    except Exception as exc:  # noqa: BLE001 — pickle raises many types
        raise LiumError(f"Arguments of {func_name} cannot be pickled for the pod: {exc}") from exc
    call = f"{func_name}(*args, **kwargs)"
    if is_async:
        call = f"asyncio.run({call})"
    return f'''#!/usr/bin/env python3
import asyncio, base64, pickle, sys, traceback

{source}

payload = {{'ok': False, 'type': 'RuntimeError', 'message': 'runner did not finish', 'traceback': ''}}
try:
    args, kwargs = pickle.loads(base64.b64decode({blob!r}))
    payload = {{'ok': True, 'result': {call}}}
except BaseException as e:
    payload = {{'ok': False, 'exc': e, 'type': type(e).__name__, 'message': str(e), 'traceback': traceback.format_exc()}}
finally:
    try:
        blob = pickle.dumps(payload, protocol=4)
    except Exception as e:
        what = 'result' if payload['ok'] else 'exception'
        payload = {{'ok': False, 'exc': None, 'type': 'PicklingError',
                   'message': f'the {{what}} of {func_name} cannot be pickled: {{e}}', 'traceback': payload.get('traceback', '')}}
        blob = pickle.dumps(payload, protocol=4)
    with open({result_path!r}, 'wb') as f:
        f.write(blob)
    if not payload['ok']:
        sys.exit(1)
'''


def _venv_path(reqs: Sequence[str]) -> str:
    """One environment per distinct requirements list, shared by every call on the pod."""
    digest = hashlib.sha1(" ".join(sorted(reqs)).encode()).hexdigest()[:10]
    return f"/tmp/lium-venv-{digest}"


def _setup_command(venv_path: str, reqs: Sequence[str]) -> str:
    """Create the venv and install ``reqs`` once; later calls find the marker and skip both.

    ``--system-site-packages`` keeps the image's own packages (the PyTorch templates ship
    torch + CUDA) visible, so ``requirements=["torch", ...]`` is satisfied in seconds
    instead of downloading torch again.
    """
    q = shlex.quote
    marker = q(f"{venv_path}/.lium-ready")
    steps = [f"python3 -m venv --system-site-packages {q(venv_path)}"]
    if reqs:
        steps.append(f"{q(venv_path)}/bin/python -m pip install -q --disable-pip-version-check {' '.join(q(r) for r in reqs)}")
    steps.append(f"touch {marker}")
    return f"if test -f {marker}; then echo LIUM_ENV_CACHED; else {' && '.join(steps)}; fi"


def _run_streaming(sdk: Lium, pod, command: str) -> Dict[str, Any]:
    """Run ``command`` on the pod, relaying its stdout/stderr to ours as it happens.

    Returns the same dict as :meth:`Lium.exec` so the caller can keep the captured output.
    """
    chunks: Dict[str, List[str]] = {"stdout": [], "stderr": []}
    streams = {"stdout": sys.stdout, "stderr": sys.stderr}
    gen = sdk.stream_exec(pod, command=command, pty=False)
    while True:
        try:
            chunk = next(gen)
        except StopIteration as stop:
            exit_code = stop.value
            break
        chunks[chunk["type"]].append(chunk["data"])
        streams[chunk["type"]].write(chunk["data"])
        streams[chunk["type"]].flush()
    return {
        "stdout": "".join(chunks["stdout"]),
        "stderr": "".join(chunks["stderr"]),
        "exit_code": exit_code,
        "success": exit_code == 0,
    }


def _raise_remote(payload: Optional[Dict[str, Any]], func_name: str, exec_result: Dict[str, Any], timeout) -> None:
    """Turn what came back from the pod into the caller's exception."""
    exit_code = exec_result.get("exit_code")
    common = dict(exit_code=exit_code, stdout=exec_result.get("stdout", ""), stderr=exec_result.get("stderr", ""))
    if payload is None:
        if timeout and exit_code == 124:  # coreutils timeout
            raise RemoteExecutionError(f"{func_name} exceeded timeout={timeout}s and was killed", **common)
        detail = exec_result.get("stderr") or exec_result.get("stdout") or "no result file and no output"
        if exit_code == -1:  # the ssh session got an exit-signal instead of an exit status
            detail = f"the process was killed by a signal (out of memory?)\n{detail}"
        raise RemoteExecutionError(f"{func_name} produced no result:\n{detail}", **common)
    cause = RemoteExecutionError(
        f"{payload['type']}: {payload['message']}\n\nRemote traceback:\n{payload.get('traceback', '')}",
        exception_type=payload["type"], remote_traceback=payload.get("traceback", ""), **common,
    )
    exc = payload.get("exc")
    if isinstance(exc, Exception):  # never re-raise a remote SystemExit/KeyboardInterrupt here
        raise exc from cause
    raise cause


class _Warm:
    def __init__(self, sdk: Lium, pod, executor: ExecutorInfo, keep_warm: float, quiet: bool = False):
        self.sdk, self.pod, self.executor, self.keep_warm, self.quiet = sdk, pod, executor, keep_warm, quiet


def _warm_key(spec: str, template_id: Optional[str]) -> str:
    count, gpu = _parse_machine(spec)
    return hashlib.sha1(f"{count}x{gpu}|{template_id or ''}".encode()).hexdigest()[:8]


def _schedule_removal(sdk: Lium, pod, delay: timedelta, say) -> None:
    """Server-side safety net: the pod goes away even if this process does not."""
    try:
        sdk.schedule_termination(_pod_ref(pod), termination_time=(datetime.now(timezone.utc) + delay).isoformat())
    except Exception as exc:  # noqa: BLE001 — a missing TTL must not fail the call
        say(f"warning: could not schedule pod removal ({exc}); remove it yourself if this process dies")


def _find_warm(sdk: Lium, key: str, say):
    """A pod this process (or an earlier one, by name) left warm for this machine spec."""
    warm = _WARM.get(key)
    live = {p.id: p for p in sdk.ps() if p.status.upper() == "RUNNING" and p.ssh_cmd}
    if warm and warm.pod.id in live:
        return warm
    _WARM.pop(key, None)
    for pod in live.values():
        if pod.name == f"lium-fn-{key}" and pod.executor:
            say(f"reusing warm pod {pod.huid} ({pod.executor.gpu_count}x{pod.executor.gpu_type} ${pod.executor.price_per_hour:.2f}/h)")
            return _Warm(sdk, pod, pod.executor, 0)
    return None


def _close_all() -> None:
    """atexit: remove pods held for `.map()`; leave `keep_warm` pods to their TTL."""
    for key, warm in list(_WARM.items()):
        _WARM.pop(key, None)
        if warm.keep_warm:
            _say(warm.quiet, "lium.machine", f"pod {warm.pod.huid} stays warm {warm.keep_warm:.0f}s for the next run, then is removed")
            continue
        try:
            warm.sdk.down(_pod_ref(warm.pod))
        except Exception:  # noqa: BLE001 — best effort at interpreter shutdown; the TTL remains
            pass


atexit.register(_close_all)


def machine(
    machine: str,
    template_id: Optional[str] = None,
    cleanup: bool = True,
    requirements: Optional[Sequence[str]] = None,
    *,
    timeout: Optional[float] = 3600,
    keep_warm: float = 0,
    local: bool = False,
    quiet: bool = False,
):
    """Decorator to execute a function on a remote Lium machine.

    Creates a new pod, sends function source code and executes it remotely,
    returns the result, and optionally cleans up the pod.

    The decorated function also offers ``f.remote(*a)`` (same as ``f(*a)``),
    ``f.local(*a)`` (run the original here), ``f.map(iterable)`` (run every item on one
    pod, rented once) and ``f.close()`` (remove the pod kept by ``keep_warm``).

    Arguments and the return value travel as pickles, so anything picklable that both
    sides can import (numpy arrays, dataclasses from an installed package, ...) works.
    Only the function's own ``def`` is sent: import what it needs inside the body and
    pass everything else as arguments. Whatever the function prints is relayed to this
    process's stdout/stderr while it runs. An exception raised on the pod is re-raised
    here with the same type; its ``__cause__`` is a :class:`RemoteExecutionError`
    carrying the remote traceback, exit code and captured output.

    Args:
        machine: ``"<count>x<gpu>"`` or ``"<gpu>"`` — e.g. ``"1xH200"``, ``"RTX4090"``,
            ``"2xA100"``. The count defaults to 1. The cheapest available node with
            exactly that many GPUs of that type is rented.
        template_id: Docker template ID (optional, uses the node's default if not specified)
        cleanup: Whether to delete the pod after execution (default: True)
        requirements: Optional iterable of pip-installable packages to install on the pod.
            They go into a venv that also sees the image's own packages, created and
            populated once per pod and reused by later calls with the same list.
        timeout: Seconds the function may run on the pod before it is killed (default 1 h;
            ``None`` for no limit). The pod is also scheduled for removal at
            ``timeout + 15 min`` (24 h when ``timeout=None``) so a caller that dies
            mid-call cannot leave it billing.
        keep_warm: Seconds the pod stays after a call for the next one — from this process
            or the next run of the script (the pod is found by name). Default 0: the pod
            is removed when the call returns. The pod is scheduled for removal server-side
            ``keep_warm + 2 min`` after each call, so nothing depends on the caller coming back.
        local: Run the function in this process instead (``LIUM_MACHINE_LOCAL=1`` does the
            same for every decorated function) — for tests and offline work.
        quiet: Suppress the one-line progress messages written to stderr.
    """

    def decorator(func):
        run_local = local or os.environ.get("LIUM_MACHINE_LOCAL") == "1"
        if not run_local:
            _check_portable(func)
            func_source = _function_source(func)
        is_async = inspect.iscoroutinefunction(func)
        key = _warm_key(machine, template_id)
        holding = [0]  # > 0 while `.map()` runs: keep the pod between items

        @wraps(func)
        def wrapper(*args, **kwargs):
            if run_local:
                return func(*args, **kwargs)
            call_id = uuid.uuid4().hex[:8]
            remote_runner = f"/tmp/lium-{call_id}.py"
            remote_result = f"/tmp/lium-{call_id}.pkl"
            runner_script = _runner_script(func_source, func.__name__, is_async, args, kwargs, remote_result)

            # Initialize SDK
            sdk = Lium()
            pod_info = None
            keep = cleanup and (keep_warm > 0 or holding[0] > 0)
            started = time.time()
            say = lambda msg: _say(quiet, func.__name__, msg)  # noqa: E731
            ttl = (timedelta(seconds=timeout) + _TTL_MARGIN) if timeout else _TTL_NO_TIMEOUT
            if keep:
                ttl += timedelta(seconds=keep_warm)

            try:
                # A pod left warm for this machine spec (by this process or the previous run) is
                # used whatever this call's keep_warm is; it is left as warm as it was found.
                warm = _find_warm(sdk, key, say) if cleanup else None
                if warm:
                    sdk, pod_info, executor = warm.sdk, warm.pod, warm.executor
                    _schedule_removal(sdk, pod_info, ttl, say)  # re-arm: this call may run up to `timeout`
                else:
                    # Step 1: Pick the cheapest node matching "<count>x<gpu>"
                    executor = _select_executor(sdk.ls(), machine)

                    # Step 2: Create pod (a fixed name lets the next run of the script find it)
                    pod_name = f"lium-fn-{key}" if keep else f"remote-{func.__name__}-{int(time.time())}"
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
                    _schedule_removal(sdk, pod_dict, ttl, say)

                    # Wait for pod to be ready
                    pod_info = sdk.wait_ready(pod_dict, timeout=_BOOT_TIMEOUT)
                    if not pod_info:
                        pod_info = _pod_ref(pod_dict)
                        raise LiumError(f"Pod {pod_name} failed to start within {_BOOT_TIMEOUT}s")
                    say(f"pod ready in {time.time() - started:.0f}s")

                # Step 3: Upload the runner script (function source + pickled arguments)
                with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                    runner_file = f.name
                    f.write(runner_script)

                # One SSH connection for the upload, the environment, the run and the download
                with sdk.ssh_session(pod_info):
                    try:
                        sdk.upload(pod_info, local=runner_file, remote=remote_runner)

                        # Steps 4-5: one round trip creates the environment and installs the
                        # requirements — or finds both already there from an earlier call.
                        reqs = [req for req in (requirements or []) if req]
                        venv_path = _venv_path(reqs)
                        venv_python = f"{venv_path}/bin/python"
                        if reqs:
                            say(f"preparing environment ({len(reqs)} package(s): {', '.join(reqs)})")
                        t_env = time.time()
                        env_result = sdk.exec(pod_info, command=_setup_command(venv_path, reqs))
                        if not env_result['success']:
                            raise LiumError(
                                f"Failed preparing the environment ({', '.join(reqs) or 'no requirements'}):\n"
                                f"{env_result['stderr'] or env_result['stdout']}"
                            )
                        if "LIUM_ENV_CACHED" in env_result['stdout']:
                            say("environment already on the pod")
                        elif reqs:
                            say(f"environment ready in {time.time() - t_env:.0f}s")

                        # Step 6: Execute runner via virtual environment python, bounded by `timeout`,
                        # relaying its output live (-u: no block buffering behind the ssh channel)
                        say("running")
                        run_cmd = f"{shlex.quote(venv_python)} -u {remote_runner}"
                        if timeout:
                            # TERM first, KILL 5 s later. (`-s KILL` would kill the process group,
                            # `timeout` included, and the ssh session would report no exit status.)
                            run_cmd = f"timeout -k 5 {int(timeout)} {run_cmd}"
                        exec_result = _run_streaming(sdk, pod_info, run_cmd)

                        # Step 7: Download the result (also when the run failed: it carries the exception)
                        payload = None
                        with tempfile.NamedTemporaryFile(delete=False) as f:
                            result_file = f.name
                        try:
                            sdk.download(pod_info, remote=remote_result, local=result_file)
                            with open(result_file, 'rb') as f:
                                payload = pickle.load(f)
                        except (OSError, IOError):
                            payload = None  # the runner never got to write it
                        except Exception as exc:  # noqa: BLE001 — unpickling: e.g. numpy missing locally
                            raise RemoteExecutionError(
                                f"the result of {func.__name__} could not be unpickled locally: {exc}. "
                                "Return plain Python types (str(), .tolist(), .cpu().numpy()) or install "
                                "the missing package here",
                                exit_code=exec_result.get("exit_code"),
                            ) from exc
                        finally:
                            if os.path.exists(result_file):
                                os.unlink(result_file)

                        if payload and payload.get('ok'):
                            elapsed = time.time() - started
                            say(f"done in {elapsed:.0f}s (~${executor.price_per_hour * elapsed / 3600:.4f})")
                            return payload['result']
                        _raise_remote(payload, func.__name__, exec_result, timeout)

                    finally:
                        # Clean up local temp file
                        os.unlink(runner_file)
                        # Remove this call's files when the pod stays alive (the venv stays: it is the cache)
                        if keep or warm or not cleanup:
                            try:
                                sdk.exec(pod_info, command=f"rm -f {remote_runner} {remote_result}")
                            except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the call's result is already in hand
                                say(f"could not remove this call's files on the pod ({exc}); they are under /tmp, the venv cache stays")

            finally:

                # Step 8: Release the pod — remove it, or keep it warm for the next call
                if pod_info and (keep or warm) and getattr(pod_info, "ssh_cmd", None):
                    stay = max(keep_warm, warm.keep_warm if warm else 0)
                    _WARM[key] = _Warm(sdk, pod_info, executor, stay, quiet)
                    if stay:
                        _schedule_removal(sdk, pod_info, timedelta(seconds=stay) + _WARM_MARGIN, say)
                        say(f"pod stays warm {stay:.0f}s")
                elif cleanup and pod_info:
                    try:
                        sdk.down(_pod_ref(pod_info))
                        say("pod removed")
                    except Exception:
                        say("warning: could not remove the pod; it is scheduled for removal server-side")

        def close():
            """Remove the pod kept warm for this machine spec (no-op when there is none)."""
            warm = _WARM.pop(key, None)
            if warm:
                warm.sdk.down(_pod_ref(warm.pod))
                _say(quiet, func.__name__, "warm pod removed")

        def map(items):  # noqa: A001 — mirrors the builtin on purpose
            """Run the function on every item of ``items``, on one pod rented once."""
            holding[0] += 1
            try:
                return [wrapper(item) for item in items]
            finally:
                holding[0] -= 1
                if holding[0] == 0 and not keep_warm and cleanup:
                    close()

        wrapper.remote = wrapper
        wrapper.local = func
        wrapper.map = map
        wrapper.close = close
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
