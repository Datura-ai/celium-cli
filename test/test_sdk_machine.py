"""`@lium.machine` against a fake client: node selection, TTL, timeout and cleanup."""

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lium.sdk import ExecutorInfo, LiumError, PodInfo
from lium.sdk import decorators as D


def _executor(gpu_type, count, price, huid="node", machine_name=None, country="US"):
    return ExecutorInfo(
        id=f"{huid}-id", huid=huid,
        machine_name=machine_name or f"NVIDIA {gpu_type}",
        gpu_type=gpu_type, gpu_count=count,
        price_per_hour=price, price_per_gpu=price / count,
        location={"country": country}, specs={}, status="online",
        docker_in_docker=False, ip="1.2.3.4",
    )


EXECUTORS = [
    _executor("A100", 8, 3.60, "eight", "NVIDIA A100-SXM4-80GB"),   # what the API lists first
    _executor("A100", 1, 1.50, "one-dear", "NVIDIA A100-SXM4-80GB"),
    _executor("A100", 1, 1.20, "one-cheap", "NVIDIA A100-SXM4-80GB"),
    _executor("H200", 1, 2.75, "h200"),
    _executor("RTX4090", 1, 0.30, "rtx", "NVIDIA GeForce RTX 4090", country="Romania"),
]


def _pod(pod_id="pod-1"):
    return PodInfo(
        id=pod_id, name="remote-fn", status="RUNNING", huid="swift-fox-c8",
        ssh_cmd="ssh root@10.0.0.1 -p 22", ports={}, created_at="", updated_at="",
        executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


class FakeLium:
    """Records every call the decorator makes and runs the uploaded runner locally."""

    ready = True
    run_exit_code = None  # None = really run the runner; int = pretend it exited with that code

    def __init__(self, sandbox: Path):
        self.sandbox = sandbox
        self.calls = []
        self.uploaded = {}

    def ls(self, **kw):
        return list(EXECUTORS)

    def up(self, **kw):
        self.calls.append(("up", kw))
        return {"id": "pod-1", "name": kw["name"]}

    def schedule_termination(self, pod, *, termination_time):
        self.calls.append(("schedule_termination", pod.id, termination_time))
        return {}

    def wait_ready(self, pod, timeout=300):
        self.calls.append(("wait_ready", pod["id"], timeout))
        return _pod(pod["id"]) if self.ready else None

    def upload(self, pod, *, local, remote):
        text = Path(local).read_text().replace("/tmp/result.json", str(self.sandbox / "result.json"))
        self.uploaded[remote] = text
        (self.sandbox / Path(remote).name).write_text(text)

    def exec(self, pod, *, command, env=None):
        self.calls.append(("exec", command))
        if "runner.py" not in command:
            return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}
        if self.run_exit_code is not None:
            return {"stdout": "", "stderr": "", "exit_code": self.run_exit_code, "success": False}
        r = subprocess.run([sys.executable, str(self.sandbox / "runner.py")], capture_output=True, text=True, timeout=30)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode, "success": r.returncode == 0}

    def download(self, pod, *, remote, local):
        Path(local).write_text((self.sandbox / Path(remote).name).read_text())

    def down(self, pod):
        self.calls.append(("down", pod.id))
        return {}


def _execs(fake):
    return [c[1] for c in fake.calls if c[0] == "exec"]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeLium(tmp_path)
    monkeypatch.setattr(D, "Lium", lambda: client)
    FakeLium.ready = True
    FakeLium.run_exit_code = None
    return client


# --- spec parsing / node selection -------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("1xH200", (1, "H200")),
    ("H200", (1, "H200")),
    ("2xRTX 4090", (2, "RTX4090")),
    ("rtx4090", (1, "RTX4090")),
    ("8 X A100", (8, "A100")),
])
def test_parse_machine(spec, expected):
    assert D._parse_machine(spec) == expected


def test_parse_machine_rejects_empty():
    with pytest.raises(ValueError):
        D._parse_machine("")


def test_select_cheapest_single_gpu_not_first_listed():
    chosen = D._select_executor(EXECUTORS, "A100")
    assert chosen.huid == "one-cheap"          # not "eight" (first in API order), not "one-dear"


def test_select_honours_count():
    assert D._select_executor(EXECUTORS, "8xA100").huid == "eight"
    assert D._select_executor(EXECUTORS, "1xH200").huid == "h200"


def test_select_matches_spaced_names():
    assert D._select_executor(EXECUTORS, "RTX 4090").huid == "rtx"
    assert D._select_executor(EXECUTORS, "RTX4090").huid == "rtx"


def test_select_names_what_exists_when_count_is_missing():
    with pytest.raises(LiumError, match=r"2xA100.*Available: 1xA100 \$1.20/h, 1xA100 \$1.50/h, 8xA100 \$3.60/h"):
        D._select_executor(EXECUTORS, "2xA100")


def test_select_unknown_type():
    with pytest.raises(LiumError, match="No node found matching machine type: B200"):
        D._select_executor(EXECUTORS, "B200")


# --- the wrapper ----------------------------------------------------------------------------
# Module-level: inspect.getsource() of a function defined inside a test is indented, which the
# runner cannot execute (see DAH-3015).

def double(x):
    return x * 2


def one():
    return 1


def slow():
    pass



def test_call_rents_cheapest_sets_ttl_bounds_run_and_cleans_up(fake, capsys):
    remote = D.machine(machine="A100", timeout=600)(double)

    assert remote(21) == 42

    up = next(c[1] for c in fake.calls if c[0] == "up")
    assert up["executor_id"] == "one-cheap-id"
    assert up["template_id"] is None                     # Lium.up resolves the node's default itself

    _, pod_id, when = next(c for c in fake.calls if c[0] == "schedule_termination")
    assert pod_id == "pod-1"
    ttl = datetime.fromisoformat(when) - datetime.now(timezone.utc)
    assert 600 + 14 * 60 < ttl.total_seconds() <= 600 + 15 * 60

    run = next(cmd for cmd in _execs(fake) if "runner.py" in cmd)
    assert run.startswith("timeout -k 5 600 ")
    assert ("down", "pod-1") in fake.calls
    assert not any(cmd.startswith("rm -rf") for cmd in _execs(fake))

    err = capsys.readouterr().err
    assert "[lium] double: renting 1xA100 $1.20/h (one-cheap, US), removal in 0.4h" in err
    assert "[lium] double: done in" in err
    assert "[lium] double: pod removed" in err


def test_timeout_none_means_no_kill_and_24h_ttl(fake):
    assert D.machine(machine="A100", timeout=None, quiet=True)(one)() == 1
    run = next(cmd for cmd in _execs(fake) if "runner.py" in cmd)
    assert not run.startswith("timeout")
    when = next(c for c in fake.calls if c[0] == "schedule_termination")[2]
    assert 23.9 * 3600 < (datetime.fromisoformat(when) - datetime.now(timezone.utc)).total_seconds() <= 24 * 3600


def test_timed_out_run_is_reported_as_timeout(fake):
    FakeLium.run_exit_code = 124

    with pytest.raises(LiumError, match="exceeded timeout=5s"):
        D.machine(machine="A100", timeout=5, quiet=True)(slow)()
    assert ("down", "pod-1") in fake.calls


def test_pod_that_never_becomes_ready_is_removed(fake):
    FakeLium.ready = False

    with pytest.raises(LiumError, match="failed to start within 300s"):
        D.machine(machine="A100", quiet=True)(one)()
    assert ("down", "pod-1") in fake.calls


def test_quiet_prints_nothing(fake, capsys):
    D.machine(machine="A100", quiet=True)(one)()
    assert capsys.readouterr().err == ""


def test_cleanup_false_keeps_pod_and_removes_venv(fake):
    D.machine(machine="A100", cleanup=False, quiet=True)(one)()
    assert not any(c[0] == "down" for c in fake.calls)
    assert any(cmd.startswith("rm -rf") for cmd in _execs(fake))
