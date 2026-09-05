"""The checks `lium doctor` runs and the pod-level ones it adds when given a pod."""

import re
import socket
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from lium.sdk import PodInfo
from lium.cli.whoami.identity import Identity

OK, WARN, FAIL = "ok", "warn", "fail"

LOW_BALANCE_USD = 1.0

# GPU names that are Blackwell (compute capability 10.x / 12.x). Wheels built for
# CUDA 12.4 or older have no kernels for them.
BLACKWELL_PATTERN = re.compile(r"\b(B200|B300|GB200|GB300|RTX\s*PRO\s*6000|RTX\s*50\d0)\b", re.IGNORECASE)
BLACKWELL_MIN_CUDA = 12.8

# ``cu126``, ``cuda12.6``, ``cuda-12.6``, ``12.6-cudnn`` in an image name or tag.
CUDA_IN_IMAGE = re.compile(r"(?:cu(?:da)?[-_ ]?)(1[1-9])[.]?(\d)(?!\d)", re.IGNORECASE)


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def identity_checks(identity: Identity) -> List[Check]:
    checks: List[Check] = []

    if identity.has_api_key:
        checks.append(Check("api_key", OK, f"{identity.api_key_fingerprint} from {identity.api_key_source}"))
    else:
        checks.append(Check("api_key", FAIL, "no API key configured", "Set LIUM_API_KEY or run 'lium init'"))

    if identity.api_reachable:
        checks.append(Check("api", OK, f"{identity.api_base_url} answered in {identity.api_latency_ms} ms"))
    elif identity.api_reachable is False:
        checks.append(Check("api", FAIL, identity.api_error or "unreachable",
                            "Check the network, LIUM_BASE_URL and that the key is valid"))
    else:
        checks.append(Check("api", WARN, "not tried (no API key)"))

    if identity.balance_usd is not None:
        if identity.balance_usd <= 0:
            checks.append(Check("balance", FAIL, f"${identity.balance_usd:.2f}", "Add funds with 'lium fund' before renting"))
        elif identity.balance_usd < LOW_BALANCE_USD:
            checks.append(Check("balance", WARN, f"${identity.balance_usd:.2f}", "Low; a pod stops when it runs out"))
        else:
            checks.append(Check("balance", OK, f"${identity.balance_usd:.2f}"))

    if not identity.ssh_key_path:
        checks.append(Check("ssh_key", FAIL, "no private key found",
                            "ssh-keygen -t ed25519, or set ssh.key_path with 'lium config set'"))
    elif not identity.ssh_public_key_found:
        checks.append(Check("ssh_key", FAIL, f"{identity.ssh_key_path} has no readable .pub file",
                            "ssh-keygen -y -f <key> > <key>.pub"))
    elif identity.ssh_key_registered is False:
        checks.append(Check("ssh_key", FAIL, f"{identity.ssh_key_path} is not registered on the account",
                            "Run 'lium up' once to register it, or 'lium ssh-keys add'"))
    elif identity.ssh_key_registered is None:
        checks.append(Check("ssh_key", WARN, f"{identity.ssh_key_path}; registration unknown"))
    else:
        checks.append(Check("ssh_key", OK, f"{identity.ssh_key_path}, registered"))

    if identity.ssh_client:
        checks.append(Check("ssh_client", OK, identity.ssh_client))
    else:
        checks.append(Check("ssh_client", FAIL, "no 'ssh' on PATH", "Install OpenSSH; 'lium ssh' and 'lium scp' need it"))

    if identity.rsync_client:
        checks.append(Check("rsync_client", OK, identity.rsync_client))
    else:
        checks.append(Check("rsync_client", WARN, "no 'rsync' on PATH", "Install rsync for 'lium rsync'"))

    checks.append(Check("cli_version", OK, f"lium {identity.cli_version}"))
    return checks


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> Optional[str]:
    """None when a TCP connection succeeds, else the reason."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return str(exc) or type(exc).__name__


def template_cuda_version(template: Dict[str, Any]) -> Optional[float]:
    """The CUDA version an image name or tag advertises, e.g. ``12.6`` from ``...-cu126``."""
    for key in ("docker_image_tag", "docker_image", "name", "template_name"):
        value = template.get(key)
        if not isinstance(value, str):
            continue
        match = CUDA_IN_IMAGE.search(value)
        if match:
            return float(f"{match.group(1)}.{match.group(2)}")
    return None


def is_blackwell(gpu_name: str) -> bool:
    return bool(BLACKWELL_PATTERN.search(gpu_name or ""))


def pod_checks(pod: PodInfo, connect=None) -> List[Check]:
    connect = connect or tcp_reachable
    checks: List[Check] = []
    label = pod.name or pod.huid

    if pod.status.upper() != "RUNNING":
        checks.append(Check("pod_status", WARN, f"{label} is {pod.status}", "Wait for RUNNING before using it"))
    else:
        checks.append(Check("pod_status", OK, f"{label} is RUNNING"))

    if not pod.host:
        checks.append(Check("pod_ssh", FAIL, f"{label} has no SSH endpoint yet", "Wait; 'lium ps' shows it once ready"))
    else:
        problem = connect(pod.host, pod.ssh_port)
        if problem is None:
            checks.append(Check("pod_ssh", OK, f"{pod.host}:{pod.ssh_port} accepts connections"))
        else:
            checks.append(Check("pod_ssh", FAIL, f"{pod.host}:{pod.ssh_port}: {problem}",
                                "The pod may still be booting; retry in a minute"))

    executor = pod.executor
    gpu_name = (executor.gpu_type or executor.gpu_model) if executor else ""
    template_cuda = template_cuda_version(pod.template or {})
    template_label = (pod.template or {}).get("name") or (pod.template or {}).get("docker_image") or "template"

    if gpu_name and template_cuda is not None and is_blackwell(gpu_name) and template_cuda < BLACKWELL_MIN_CUDA:
        checks.append(Check(
            "template_arch", WARN,
            f"{gpu_name} is Blackwell but {template_label} is built for CUDA {template_cuda}",
            f"Use a cu{int(BLACKWELL_MIN_CUDA * 10)}+ image or 'pip install torch --index-url "
            "https://download.pytorch.org/whl/cu130' inside the pod",
        ))
    elif executor and executor.max_cuda_version and template_cuda is not None and template_cuda > executor.max_cuda_version:
        checks.append(Check(
            "template_arch", WARN,
            f"{template_label} is built for CUDA {template_cuda} but the node's driver supports up to "
            f"{executor.max_cuda_version}",
            "Pick a template with an older CUDA build, or a node with a newer driver",
        ))
    elif gpu_name and template_cuda is not None:
        checks.append(Check("template_arch", OK, f"{gpu_name} with CUDA {template_cuda} image"))

    return checks


def overall(checks: List[Check]) -> str:
    statuses = {c.status for c in checks}
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return OK
