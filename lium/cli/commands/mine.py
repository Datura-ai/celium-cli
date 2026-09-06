"""Mine command for setting up a compute subnet executor/miner."""

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

import click
from rich import box
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ..utils import console, handle_errors, timed_step_status


# --------------------------
# Helpers
# --------------------------
def _get_gpu_info() -> dict:
    """Get GPU information using nvidia-smi."""
    # Query only the GPU name field to get clean output
    out, _ = _run("nvidia-smi --query-gpu=name --format=csv,noheader")
    lines = out.strip().split('\n')

    if lines and lines[0]:
        # Get the first GPU's name (all GPUs in a system are typically the same model)
        gpu_name = lines[0].strip()
        # Count the number of GPUs
        gpu_count = len(lines)
        return {"gpu_count": gpu_count, "gpu_type": gpu_name}
    
    return {"gpu_count": 0, "gpu_type": None}


def _get_public_ip() -> str:
    """Get the public IP address (IPv4 only)."""
    # Try multiple services for redundancy, requesting IPv4 explicitly
    services = [
        "https://api.ipify.org?format=text",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip"
    ]

    for service in services:
        out, _ = _run(f"curl -4 -s {service}")
        ip = out.strip()
        # Validate IPv4 format (strict check for valid octets)
        if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", ip):
            # Additional check: each octet must be 0-255
            octets = ip.split('.')
            if all(0 <= int(octet) <= 255 for octet in octets):
                return ip
    return "Unable to determine"

def _run(cmd: list | str, check=True, capture=True, cwd: Optional[str] = None) -> Tuple[str, str]:
    import subprocess
    if isinstance(cmd, list):
        cmd_str = " ".join(cmd)
    else:
        cmd_str = cmd
    result = subprocess.run(
        cmd_str,
        shell=True,
        cwd=cwd,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd_str}\n"
            f"--- stdout ---\n{(result.stdout or '')[:4000]}\n"
            f"--- stderr ---\n{(result.stderr or '')[:4000]}"
        )
    return (result.stdout or ""), (result.stderr or "")


def _exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _show_setup_summary():
    table = Table(title="Node Setup Plan", show_header=False, box=box.SIMPLE_HEAVY)
    table.add_column("Step", style="cyan", no_wrap=True)
    table.add_column("What happens")
    table.add_row("1", "Clone or update compute-subnet repo")
    table.add_row("2", "Install node dependencies")
    table.add_row("3", "Prerequisite check (Docker, NVIDIA GPU)")
    table.add_row("4", "Configure node .env (ports, hotkey)")
    table.add_row("5", "Start node with docker compose")
    table.add_row("6", "Validate node configuration")
    console.print(table)
    console.print()


# --------------------------
# Actions
# --------------------------
def _clone_or_update_repo(target_dir: Path, branch: str):
    if target_dir.exists():
        if (target_dir / ".git").exists():
            _run("git fetch --all", cwd=str(target_dir))
            _run(f"git checkout {branch}", cwd=str(target_dir))
            _run(f"git pull origin {branch}", cwd=str(target_dir))

    else:
        _run(f"git clone --branch {branch} https://github.com/Datura-ai/lium-io.git {target_dir}")


def _check_prereqs():
    if not _exists("nvidia-smi"):
        raise Exception("NVIDIA GPU driver not found (nvidia-smi missing)")

    _run("nvidia-smi --query-gpu=name --format=csv,noheader")

    if not _exists("nvidia-container-cli"):
        raise Exception("NVIDIA Container Toolkit not found (required for Docker GPU access)")

    if not _exists("docker"):
        raise Exception("Docker not found")

    _run("docker info")


def _install_executor_tools(compute_dir: Path):
    script = compute_dir / "scripts" / "install_executor_on_ubuntu.sh"
    if not script.exists():
        raise Exception(f"Install script not found at {script}")

    _run(f"bash {script}")


def _setup_executor_env(
    executor_dir: str | Path,
    *,
    hotkey: str,
    internal_port: int = 8080,
    external_port: int = 8080,
    ssh_port: int = 2200,
    ssh_public_port: str = "",
    port_range: str = "",
):
    """
    Render neurons/executor/.env from .env.template with provided values.

    - Never prompts.
    - Preserves unknown lines/keys from the template.
    - Ensures required keys exist even if missing in template.
    """
    executor_dir = Path(executor_dir)
    env_t = executor_dir / ".env.template"
    env_f = executor_dir / ".env"

    if not env_t.exists():
        raise Exception(f"Template file not found at {env_t}")

    # light sanity checks (don't be strict)
    def _valid_port(p: int) -> bool:
        return isinstance(p, int) and 1 <= p <= 65535

    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{40,60}", hotkey or ""):
        raise Exception(f"Invalid hotkey format: {hotkey}")

    for p, name in [(internal_port, "INTERNAL_PORT"),
                    (external_port, "EXTERNAL_PORT"),
                    (ssh_port, "SSH_PORT")]:
        if not _valid_port(p):
            raise Exception(f"Invalid port {name}={p} (must be 1-65535)")

    # read, rewrite, preserve
    src_lines = env_t.read_text().splitlines()
    out_lines = []
    seen = set()

    def put(k: str, v: str | int):
        nonlocal out_lines, seen
        out_lines.append(f"{k}={v}")
        seen.add(k)

    for line in src_lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            out_lines.append(line)
            continue

        k, _ = line.split("=", 1)
        if k == "MINER_HOTKEY_SS58_ADDRESS":
            put(k, hotkey)
        elif k == "INTERNAL_PORT":
            put(k, internal_port)
        elif k == "EXTERNAL_PORT":
            put(k, external_port)
        elif k == "SSH_PORT":
            put(k, ssh_port)
        elif k == "SSH_PUBLIC_PORT":
            # only write if provided; otherwise keep template as-is or blank it
            if ssh_public_port:
                put(k, ssh_public_port)
            else:
                out_lines.append(line)  # preserve whatever template had
        elif k == "RENTING_PORT_RANGE":
            if port_range:
                put(k, port_range)
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)  # unknown key: preserve

    # ensure required keys exist even if template lacked them
    required = {
        "MINER_HOTKEY_SS58_ADDRESS": hotkey,
        "INTERNAL_PORT": internal_port,
        "EXTERNAL_PORT": external_port,
        "SSH_PORT": ssh_port,
    }
    for k, v in required.items():
        if k not in seen and not any(l.startswith(f"{k}=") for l in out_lines):
            out_lines.append(f"{k}={v}")

    env_f.write_text("\n".join(map(str, out_lines)) + "\n")


def _listening_process(port: int) -> str:
    """Best-effort 'who owns this port' hint from ``ss -ltnp`` (Linux only)."""
    if not _exists("ss"):
        return ""
    out, _ = _run("ss -ltnp", check=False)
    # Without root, ss hides the owner of other users' sockets (e.g. docker-proxy).
    if "users:" not in out and _exists("sudo"):
        sudo_out, _ = _run("sudo -n ss -ltnp", check=False)
        out = sudo_out or out
    for line in out.splitlines():
        if re.search(rf"[:\]]{port}\s", line):
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            if m:
                return f"{m.group(1)} pid {m.group(2)}"
            return "unknown process"
    return ""


def _port_in_use(port: int) -> bool:
    """True when nothing on this host can still bind ``0.0.0.0:<port>``.

    A plain bind (no SO_REUSEADDR) also fails when a listener is bound to a
    single interface, which is exactly what ``docker compose up`` would hit.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def _host_ports_from_answers(answers: dict) -> dict[str, int]:
    """Host ports docker compose will publish, from the gathered answers.

    The public SSH port is only a NAT forward on the provider's router, so
    it is not bound on this host and is not checked.
    """
    ports: dict[str, int] = {}
    for label, key in (("service port", "external_port"), ("SSH port", "ssh_port")):
        v = str(answers.get(key) or "").strip()
        if v.isdigit():
            ports[label] = int(v)
    return ports


def _check_ports_free(ports: dict[str, int]) -> None:
    """Fail before ``docker compose up`` when a configured host port is taken.

    ``ports`` maps a human label to the port number, e.g.
    ``{"service port": 8080, "SSH port": 2200}``. Without this check the
    executor container enters a restart loop and the caller only sees the
    health check time out three minutes later.
    """
    for label, port in ports.items():
        if not port or not _port_in_use(port):
            continue
        owner = _listening_process(port)
        who = f" ({owner})" if owner else ""
        raise Exception(
            f"Port {port} ({label}) is already in use on this host{who}. "
            f"Free it or pick another port (run `lium mine` without --auto to choose ports)."
        )


def _compose_diagnostics(executor_dir: Path, tail: int = 30) -> str:
    """``docker compose ps`` + the last log lines of the executor services.

    Used when the health check times out so the actual failure (port
    conflict, image pull error, bad .env) is on screen instead of only
    'timed out'.
    """
    parts = []
    ps, _ = _run("docker compose ps", check=False, cwd=str(executor_dir))
    if ps.strip():
        parts.append("--- docker compose ps ---\n" + ps.strip())
    logs, err = _run(
        f"docker compose logs --no-color --tail {tail} executor executor-runner",
        check=False,
        cwd=str(executor_dir),
    )
    text = (logs or "") + (err or "")
    if text.strip():
        parts.append(f"--- last {tail} log lines (executor, executor-runner) ---\n" + text.strip()[-4000:])
    return "\n".join(parts)


def _start_executor(executor_dir: Path, wait_secs: int = 180):
    # Start using the default docker-compose.yml
    _run("docker compose up -d", capture=True, cwd=str(executor_dir))

    # Wait for the executor service to be fully healthy
    start = time.time()
    
    while time.time() - start < wait_secs:
        # Get the container name/ID for the executor service
        out, _ = _run("docker compose -f docker-compose.app.yml ps -q executor", cwd=str(executor_dir))
        if not out.strip():
            time.sleep(2)
            continue
            
        container_id = out.strip()
        
        # Check the health status directly using docker inspect
        out, _ = _run(f"docker inspect --format='{{{{.State.Health.Status}}}}' {container_id}")
        
        if out.strip():
            health_status = out.strip()
            # Only return true if explicitly healthy
            if health_status == "healthy":
                return
        time.sleep(3)
    diag = _compose_diagnostics(executor_dir)
    raise Exception(
        f"Node health check timed out after {wait_secs}s."
        + (f"\n{diag}" if diag else "")
    )

def _apply_env_overrides(
    executor_dir: Path,
    internal: str, external: str, ssh: str, ssh_pub: str, rng: str
):
    env_f = executor_dir / ".env"
    content = env_f.read_text().splitlines()
    def set_or_append(key, val):
        nonlocal content
        pat = f"{key}="
        for i, line in enumerate(content):
            if line.startswith(pat):
                content[i] = f"{pat}{val}"
                break
        else:
            content.append(f"{pat}{val}")
    set_or_append("INTERNAL_PORT", internal)
    set_or_append("EXTERNAL_PORT", external)
    set_or_append("SSH_PORT", ssh)
    if ssh_pub:
        set_or_append("SSH_PUBLIC_PORT", ssh_pub)
    if rng:
        set_or_append("RENTING_PORT_RANGE", rng)
    env_f.write_text("\n".join(content) + "\n")

def _gather_inputs(
    hotkey: Optional[str],
    auto: bool,
) -> dict:
    """Ask everything up-front; return a dict of resolved inputs."""
    answers = {}
    if auto:
        # Auto mode - use all defaults
        answers["hotkey"] = hotkey or ""
        answers.update(dict(
            internal_port="8080",
            external_port="8080",
            ssh_port="2200",
            ssh_public_port="",
            port_range=""
        ))
    else:
        # Show informative header about port configuration
        console.print("\n[bold]We're setting up how your node can be reached.[/bold]\n")
        console.print("• [cyan]Service port[/cyan] → where the node's HTTP API listens (default 8080).")
        console.print("• [cyan]Node SSH port[/cyan] → used by validators to SSH into the container (default 2200).")
        console.print("• [cyan]Public SSH port[/cyan] → only if your server is behind NAT and you forward a different public port.")
        console.print("• [cyan]Renting port range[/cyan] → optional, used only if your firewall limits outbound ports.\n")
        
        if not hotkey:
            hotkey = Prompt.ask("Miner hotkey SS58 address")
        else:
            console.print(f"Miner hotkey SS58 address: [yellow]{hotkey}[/yellow]\n")
        answers["hotkey"] = hotkey or ""

        def ask_port(label, default):
            while True:
                v = Prompt.ask(label, default=str(default))
                if not v:  # Allow empty for optional ports
                    return ""
                if v.isdigit() and 1 <= int(v) <= 65535:
                    return v
                console.warning("Port must be an integer between 1 and 65535.")
        
        # Service ports
        service_port = ask_port("Service port (where the node API will be reachable)", 8080)
        answers["internal_port"] = service_port
        answers["external_port"] = service_port  # Set external same as internal
        answers["ssh_port"] = ask_port("Node SSH port (used by validator to SSH into the container)", 2200)
        
        # Optional ports
        ssh_public = Prompt.ask("Public SSH port (optional, only if behind NAT and forwarding a different port)", default="")
        answers["ssh_public_port"] = ssh_public if ssh_public and ssh_public.isdigit() else ""
        
        answers["port_range"] = Prompt.ask("Renting port range (optional, e.g. 2000-2005 or 2000,2001). Leave empty if all ports open", default="")

    return answers


PREFLIGHT_IMAGE = "daturaai/lium-validator:latest"


class _StepMessage:
    """Step label the spinner re-reads on every redraw, so a detail can change while it runs."""

    def __init__(self, text: str):
        self.text = text
        self.detail = ""

    def __str__(self) -> str:
        return f"{self.text} ({self.detail})" if self.detail else self.text


def _start_preflight_pull():
    """Pull the preflight image in the background.

    The image is ~900 MB (30–60 s on a typical provider link). Started right after the
    prerequisites pass, the pull overlaps steps 4–5 (env, compose up, health wait) instead
    of being paid inside "Validating node".
    """
    import subprocess

    return subprocess.Popen(
        f"docker pull {PREFLIGHT_IMAGE}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _validate_executor(extra_args=None, on_check=None):
    """Run the validator's preflight image and raise on a failed verdict.

    ``on_check(name)`` is called as each check starts (GPU configuration, matrix
    work-proof, VerifyX), read from the image's ``--debug`` log on stderr; the JSON
    verdict stays on stdout.
    """
    import subprocess

    docker_cmd = f"docker run --rm --gpus all {PREFLIGHT_IMAGE} --debug"
    if extra_args:
        docker_cmd += " " + " ".join(extra_args)

    # errors="replace": in --debug mode the matrix check echoes its raw cipher bytes
    # on stdout ahead of the JSON verdict.
    proc = subprocess.Popen(
        docker_cmd,
        shell=True,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    err_tail: list[str] = []
    for line in proc.stderr:
        err_tail = (err_tail + [line.rstrip()])[-20:]
        m = re.search(r"Running check: (.+)$", line)
        if m and on_check:
            on_check(m.group(1).strip())
    out = proc.stdout.read()
    proc.wait()

    result = _preflight_verdict(out)
    if result is None:
        raise Exception(
            "Preflight image produced no verdict (exit %s):\n%s"
            % (proc.returncode, "\n".join(err_tail)[-2000:])
        )
    if not result.get("passed", False):
        raise Exception(result.get("message", ""))


def _preflight_verdict(stdout: str) -> Optional[dict]:
    """The image's JSON verdict: the last object that starts at a line beginning on stdout."""
    for m in reversed(list(re.finditer(r"^\{", stdout, re.M))):
        try:
            return json.loads(stdout[m.start():])
        except ValueError:
            continue
    return None


# --------------------------
# CLI
# --------------------------
@click.command("mine", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--hotkey", "-k", help="Miner hotkey SS58 address")
@click.option("--dir", "-d", "dir_", default="compute-subnet", help="Target directory")
@click.option("--branch", "-b", default="main")
@click.option("--auto", "-a", is_flag=True)
@click.option("--verbose", "-v", is_flag=True, help="Show the plan banner")
@click.pass_context
@handle_errors
def mine_command(ctx, hotkey, dir_, branch, auto, verbose):
    if verbose:
        _show_setup_summary()   # keep the banner only when asked

    answers = _gather_inputs(hotkey, auto)
    target_dir = Path(dir_).absolute()

    TOTAL_STEPS = 6

    try:
        with timed_step_status(1, TOTAL_STEPS, "Ensuring repository"):
            _clone_or_update_repo(target_dir, branch)

        with timed_step_status(2, TOTAL_STEPS, "Installing node tools"):
            _install_executor_tools(target_dir)

        with timed_step_status(3, TOTAL_STEPS, "Checking prerequisites"):
            _check_prereqs()

        # Docker is confirmed; fetch the preflight image while steps 4–5 run.
        preflight_pull = _start_preflight_pull()

        with timed_step_status(4, TOTAL_STEPS, "Configuring environment"):
            executor_dir = target_dir / "neurons" / "executor"
            if not executor_dir.exists():
                raise Exception(f"Node directory not found at {executor_dir}")

            _setup_executor_env(
                str(executor_dir),
                hotkey=answers["hotkey"],
            )

            _apply_env_overrides(
                executor_dir,
                internal=answers["internal_port"],
                external=answers["external_port"],
                ssh=answers["ssh_port"],
                ssh_pub=answers["ssh_public_port"],
                rng=answers["port_range"],
            )

            # A taken host port makes `docker compose up` loop on
            # "address already in use" and the health check below time out
            # with no explanation. Catch it here, before the 3-minute wait.
            _check_ports_free(_host_ports_from_answers(answers))

        with timed_step_status(5, TOTAL_STEPS, "Starting node"):
            _start_executor(executor_dir)

        console.dim(
            "Validation runs the validator's preflight image: GPU check, matrix "
            "work-proof and VerifyX (RAM, disk throughput, network). Typically 2–4 minutes."
        )
        step6 = _StepMessage("Validating node")
        with timed_step_status(6, TOTAL_STEPS, step6):
            if preflight_pull.poll() is None:
                step6.detail = "pulling preflight image"
                preflight_pull.wait()

            def _show_check(name: str) -> None:
                step6.detail = name

            # Pass any extra arguments to the validator
            _validate_executor(ctx.args if ctx.args else None, on_check=_show_check)

    except Exception as e:
        console.error(f"❌ {e}")
        return

    # Get executor details for summary
    gpu_info = _get_gpu_info()
    public_ip = _get_public_ip()
    
    # Get the external port from answers
    external_port = answers.get("external_port", "8080")
    
    console.success("\n✨ Node setup complete!")
    console.print()
    
    details_table = Table(show_header=False, box=None)
    details_table.add_column("Key", style="cyan")
    details_table.add_column("Value", style="white")
    
    details_table.add_row("📍 Endpoint", f"{public_ip}:{external_port}")
    details_table.add_row("🎮 GPU", f"{gpu_info['gpu_count']}×{gpu_info['gpu_type']}")
    details_table.add_row("📂 Directory", str(executor_dir))
    details_table.add_row("🔑 Hotkey", answers.get("hotkey", "Not set")[:20] + "..." if len(answers.get("hotkey", "")) > 20 else answers.get("hotkey", "Not set"))
    
    console.print(Panel(details_table, title="[bold]Node Details[/bold]", border_style="green"))
    
    # Generate URL for adding executor via web interface
    from urllib.parse import urlencode
    
    # Build query parameters
    params = {
        'action': 'add',
        'gpu_type': gpu_info.get('gpu_type', 'Unknown'),
        'ip_address': public_ip,
        'port': external_port,
        'gpu_count': gpu_info.get('gpu_count', 0)
    }
    
    # Build full URL with proper encoding
    add_url = f"https://provider.lium.io/nodes?{urlencode(params)}"
    
    console.print("\n[bold cyan]Register this node in the Provider Portal:[/bold cyan]")
    console.print(f"[yellow]{add_url}[/yellow]\n")
    console.print("[bold cyan]…or from this terminal:[/bold cyan]")
    console.print(f"[yellow]{_provider_add_command(gpu_info, public_ip, external_port)}[/yellow]")
    console.dim(_registration_note())


def _provider_add_command(gpu_info: dict, public_ip: str, external_port: str | int) -> str:
    """The `lium provider node add` equivalent of the portal Add-Node modal."""
    import shlex

    gpu_type = gpu_info.get("gpu_type") or "Unknown"
    return (
        "lium provider node add "
        f"--gpu-type {shlex.quote(gpu_type)} --gpu-count {gpu_info.get('gpu_count', 0)} "
        f"--ip {public_ip} --port {external_port} --yes"
    )


def _registration_note() -> str:
    return (
        "Validators only reach nodes of providers with a running coordinator: opt in to the "
        "Lium Central Provider Server (`lium provider config opt-in --yes`, or Profile Settings "
        "in the portal) or run a self-hosted provider. Until then the node stays "
        "VALIDATION_PENDING. The first validation takes roughly 15 minutes; add --price to "
        "`node add` to override the default price."
    )
