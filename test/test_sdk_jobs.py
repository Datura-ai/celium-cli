"""Background jobs: run_background() -> Job, re-attach by name, wait_for_port, logs, kill.

Every agent that served a model on a pod wrote its own `setsid nohup … &` plus a poll loop,
and at least one wasted minutes because its readiness check matched its own wrapper. The SDK
owns that now: a Job has a PID file, an exit-code file, a log, and a port probe that stops the
moment the process dies.
"""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import shlex

import pytest

from lium.sdk import Config, ExecutorInfo, Job, Lium, LiumError, LiumNotFoundError, PodInfo
from lium.sdk.jobs import (
    build_job_launcher,
    build_port_probe,
    build_status_probe,
    job_paths,
    parse_status,
    validate_job_name,
)


def _pod() -> PodInfo:
    return PodInfo(
        id="pod-1", name="serve", huid="swift-fox-c8", status="RUNNING", ssh_cmd="ssh root@1.2.3.4 -p 20299",
        ports={"22": 20299, "8000": 40123}, created_at="2026-01-01T00:00:00Z", updated_at="",
        executor=ExecutorInfo(
            id="exec-1", huid="brave-otter-11", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
            gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={}, specs={}, status="active",
            docker_in_docker=False, ip="1.2.3.4",
        ),
        template={"id": "tpl-1"}, removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


class _Client(Lium):
    def __init__(self):
        super().__init__(Config(api_key="test"))

    def _request(self, method, endpoint, **kwargs):  # pragma: no cover - nothing here talks to the API
        raise AssertionError(f"unexpected API call {method} {endpoint}")


class _Stream:
    def __init__(self, text: str = "", exit_code: int = 0):
        self._text = text
        self.channel = SimpleNamespace(exit_status_ready=lambda: True, recv_exit_status=lambda: exit_code, close=lambda: None)

    def read(self):
        return self._text.encode()

    def close(self):
        pass


def _ssh_answering(monkeypatch, client, answers):
    """Replace the SSH session with one that answers successive commands from ``answers``.

    Each answer is a string (stdout, exit 0), a ``(stdout, exit_code)`` tuple, or an
    exception instance to raise when connecting. The last answer repeats.
    """
    sent: list[str] = []
    queue = list(answers)

    def _next():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    class _Ssh:
        def exec_command(self, command, **kwargs):
            sent.append(command)
            answer = _next()
            stdout, code = answer if isinstance(answer, tuple) else (answer, 0)
            return _Stream(), _Stream(stdout, code), _Stream("")

    @contextmanager
    def fake_connection(pod, timeout=30):
        if isinstance(queue[0], Exception):
            raise _next()
        yield _Ssh()

    monkeypatch.setattr(client, "ssh_connection", fake_connection)
    return sent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# --- the remote command lines -----------------------------------------------------------------

def test_launcher_writes_pid_and_exit_files_and_detaches_from_the_session():
    line = build_job_launcher("vllm serve m --port 8000", name="vllm", job_dir="/workspace/logs")

    assert line.startswith("mkdir -p /workspace/logs || exit 1; ")
    assert "kill -0 \"$(cat /workspace/logs/vllm.pid)\"" in line and "exit 3" in line
    assert "rm -f /workspace/logs/vllm.exit; " in line
    assert "printf %s 'vllm serve m --port 8000' > /workspace/logs/vllm.cmd; " in line
    assert "nohup setsid bash -c 'bash -lc '\"'\"'vllm serve m --port 8000'\"'\"'; echo $? > /workspace/logs/vllm.exit'" in line
    assert line.endswith("> /workspace/logs/vllm.log 2>&1 < /dev/null & echo $! > /workspace/logs/vllm.pid; echo $!")


def test_launcher_changes_directory_first_when_asked():
    line = build_job_launcher("python train.py", name="train", workdir="/workspace/repo")

    assert "cd /workspace/repo && python train.py" in line


def test_status_and_port_probes_are_bash_only():
    assert build_status_probe(4242, "/workspace/logs/j.exit") == (
        'if [ -f /workspace/logs/j.exit ]; then echo "exited $(cat /workspace/logs/j.exit)"; '
        "elif kill -0 4242 2>/dev/null; then echo running; else echo gone; fi"
    )
    assert build_port_probe(8000) == (
        "if timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8000' 2>/dev/null; then echo 'port open'; else echo 'port closed'; fi"
    )


def test_parse_status_reads_the_three_states():
    assert parse_status("exited 0\n") == {"state": "exited", "exit_code": 0}
    assert parse_status("exited 137\nport closed\n") == {"state": "exited", "exit_code": 137}
    assert parse_status("running\nport closed\n") == {"state": "running", "exit_code": None}
    assert parse_status("gone\n") == {"state": "gone", "exit_code": None}
    assert parse_status("") == {"state": "unknown", "exit_code": None}


@pytest.mark.parametrize("bad", ["", "../etc", "a b", "-lead", "x" * 65, "semi;colon"])
def test_job_names_are_file_name_stems(bad):
    with pytest.raises(ValueError, match="Invalid job name"):
        validate_job_name(bad)


def test_job_paths_sit_next_to_each_other():
    assert job_paths("vllm", "/workspace/logs/") == {
        "log_path": "/workspace/logs/vllm.log", "pid_file": "/workspace/logs/vllm.pid",
        "exit_file": "/workspace/logs/vllm.exit", "cmd_file": "/workspace/logs/vllm.cmd",
    }


# --- run_background ---------------------------------------------------------------------------

def test_run_background_returns_a_job_with_pid_and_paths(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["31337\n"])

    job = client.run_background(_pod(), "vllm serve m --port 8000", name="vllm")

    assert isinstance(job, Job)
    assert (job.name, job.pid, job.command) == ("vllm", 31337, "vllm serve m --port 8000")
    assert job.log_path == "/workspace/logs/vllm.log" and job.pid_file == "/workspace/logs/vllm.pid"
    assert "nohup setsid bash -c" in sent[0] and sent[0].endswith("echo $!")


def test_run_background_names_the_job_after_the_time_by_default(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["7\n"])

    job = client.run_background(_pod(), "sleep 1")

    assert job.name.startswith("job-") and job.name.endswith("Z")
    assert job.log_path == f"/workspace/logs/{job.name}.log"


def test_run_background_exports_env_inside_the_job_shell(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["7\n"])

    job = client.run_background(_pod(), "run", name="j", env={"HF_HOME": "/workspace/hf"})

    assert "export HF_HOME=/workspace/hf && run" in sent[0]
    # The .cmd file records the command as given; env values stay out of it, so
    # job() returns the same command and no secret is written to disk.
    assert "printf %s run > /workspace/logs/j.cmd" in sent[0]
    assert job.command == "run"


def test_launcher_env_precedes_workdir_and_stays_out_of_the_cmd_file():
    line = build_job_launcher("python train.py", name="t", workdir="/workspace/repo", env={"TOKEN": "s3cret"})

    assert "export TOKEN=s3cret && cd /workspace/repo && python train.py" in line
    assert "printf %s 'python train.py' > /workspace/logs/t.cmd" in line
    assert line.count("s3cret") == 1


def test_launcher_env_values_are_quoted_and_keys_validated():
    """A value with quotes or `$(` is a literal inside the job shell, never shell text."""
    line = build_job_launcher("run", name="t", env={"MSG": 'say "hi" $(id)'})

    # the job shell (bash -lc <inner>) sees `export MSG='say "hi" $(id)' && run`, the value one literal word
    inner = "export MSG=" + shlex.quote('say "hi" $(id)') + " && run"
    wrapper = f"bash -lc {shlex.quote(inner)}; echo $? > /workspace/logs/t.exit"
    assert f"bash -c {shlex.quote(wrapper)}" in line

    with pytest.raises(ValueError, match="Invalid environment variable name"):
        build_job_launcher("run", name="t", env={"BAD-NAME": "x"})
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        build_job_launcher("run", name="t", env={"X; rm -rf /": "x"})


def test_run_background_refuses_a_name_whose_job_is_still_running(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, [("", 3)])

    with pytest.raises(LiumError, match="Could not start job vllm"):
        client.run_background(_pod(), "vllm serve m", name="vllm")


def test_run_background_fails_loudly_when_no_pid_came_back(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["", ])

    with pytest.raises(LiumError, match="launcher printed no PID"):
        client.run_background(_pod(), "run", name="j")


def test_run_background_rejects_a_bad_name_before_touching_the_pod(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["7\n"])

    with pytest.raises(ValueError):
        client.run_background(_pod(), "run", name="../escape")

    assert sent == []


def test_job_to_dict_is_json_serialisable():
    job = Job(_Client(), _pod(), name="vllm", pid=5, command="vllm serve m")

    data = json.loads(json.dumps(job.to_dict()))

    assert data["name"] == "vllm" and data["pid"] == 5 and data["pod_id"] == "pod-1"
    assert data["log_path"] == "/workspace/logs/vllm.log" and data["exit_file"] == "/workspace/logs/vllm.exit"


# --- re-attaching -----------------------------------------------------------------------------

def test_job_reattaches_by_name_from_the_pid_and_cmd_files(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["4242\n\n---cmd---vllm serve m --port 8000"])

    job = client.job(_pod(), "vllm")

    assert (job.pid, job.command, job.log_path) == (4242, "vllm serve m --port 8000", "/workspace/logs/vllm.log")
    assert "cat /workspace/logs/vllm.pid" in sent[0] and "cat /workspace/logs/vllm.cmd" in sent[0]


def test_job_reattach_reports_a_missing_job(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, [("---cmd---", 1)])

    with pytest.raises(LiumNotFoundError, match="No job named vllm"):
        client.job(_pod(), "vllm")


def test_jobs_lists_every_pid_file(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["vllm 4242\ntrain 99\nnot a job line\n"])

    jobs = client.jobs(_pod())

    assert [(j.name, j.pid) for j in jobs] == [("vllm", 4242), ("train", 99)]


# --- status, poll, wait -----------------------------------------------------------------------

def test_status_poll_and_is_running(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n", "exited 0\n"])
    job = Job(client, _pod(), name="j", pid=1, command="x")

    assert job.is_running() is True
    assert job.poll() == 0


def test_poll_raises_when_the_process_is_gone_without_an_exit_code(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["gone\n"])

    with pytest.raises(LiumError, match="gone without an exit code"):
        Job(client, _pod(), name="j", pid=1, command="x").poll()


def test_wait_returns_the_exit_code_when_the_job_ends(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n", "running\n", "exited 2\n"])

    assert Job(client, _pod(), name="j", pid=1, command="x").wait(timeout=100) == 2


def test_wait_times_out_and_names_the_log(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n"])
    clock = iter([0.0, 0.0, 1000.0, 1000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="/workspace/logs/j.log"):
        Job(client, _pod(), name="j", pid=1, command="x").wait(timeout=10)


# --- wait_for_port ----------------------------------------------------------------------------

def test_wait_for_port_returns_once_the_port_answers(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["running\nport closed\n", "running\nport closed\n", "running\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=600)

    assert len(sent) == 3
    assert "kill -0 1" in sent[0] and "/dev/tcp/127.0.0.1/8000" in sent[0]


def test_wait_for_port_fails_at_once_when_the_job_died_and_shows_the_log(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 1\nport closed\n", "Traceback: CUDA out of memory\n"])

    with pytest.raises(LiumError, match="exited with code 1 before port 8000") as info:
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=600)

    assert "CUDA out of memory" in str(info.value)


def test_wait_for_port_does_not_take_a_port_held_by_someone_else_for_a_dead_job(monkeypatch):
    """The job died; another process answers on the port. That is not a ready server."""
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 1\nport open\n", "boom\n"])

    with pytest.raises(LiumError, match="exited with code 1 before port 8000.*not by this job"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_accepts_a_launcher_that_forked_its_server_and_exited_cleanly(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 0\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_fails_when_the_process_vanished(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["gone\nport closed\n", ""])

    with pytest.raises(LiumError, match="is gone before port 8000"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_times_out_while_the_job_still_runs(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\nport closed\n"])
    clock = iter([0.0, 5.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="did not answer within 60s.*job vllm is running"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=60)


def test_wait_for_port_keeps_polling_through_a_dropped_ssh_connection(monkeypatch):
    import paramiko

    client = _Client()
    _ssh_answering(monkeypatch, client, [paramiko.SSHException("banner"), "running\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_probes_another_host_when_asked(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["running\nport open\n"])

    Job(client, _pod(), name="j", pid=1, command="x").wait_for_port(9000, host="0.0.0.0")

    assert "/dev/tcp/0.0.0.0/9000" in sent[0]


def test_client_wait_for_port_without_a_job(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["port closed\n", "port open\n"])

    client.wait_for_port(_pod(), 8000)

    assert len(sent) == 2 and "kill -0" not in sent[0]


def test_client_wait_for_port_times_out(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["port closed\n"])
    clock = iter([0.0, 1.0, 999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="Port 8000 on pod serve"):
        client.wait_for_port(_pod(), 8000, timeout=5)


# --- wait_ready(ready_port=) ------------------------------------------------------------------

def test_wait_ready_with_ready_port_waits_for_running_then_for_the_port(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "ps", lambda: [_pod()])
    waited = []
    monkeypatch.setattr(client, "wait_for_port", lambda pod, port, **kw: waited.append((pod.id, port, round(kw["timeout"]))))

    pod = client.wait_ready("pod-1", timeout=300, ready_port=8000)

    assert pod.id == "pod-1" and waited == [("pod-1", 8000, 300)]


def test_wait_ready_with_ready_port_returns_none_when_the_port_never_answers(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "ps", lambda: [_pod()])

    def never(pod, port, **kw):
        raise TimeoutError("no")

    monkeypatch.setattr(client, "wait_for_port", never)

    assert client.wait_ready("pod-1", timeout=300, ready_port=8000) is None


# --- logs and kill ----------------------------------------------------------------------------

def test_logs_reads_the_whole_file_or_a_tail(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["line1\nline2\n"])
    job = Job(client, _pod(), name="j", pid=1, command="x")

    assert job.logs() == "line1\nline2\n"
    assert job.logs(tail=40) == "line1\nline2\n"
    assert sent[0].startswith("cat /workspace/logs/j.log") and sent[1].startswith("tail -n 40 /workspace/logs/j.log")


def test_kill_signals_the_process_group(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["", ])
    job = Job(client, _pod(), name="j", pid=4242, command="x")

    assert job.kill() is True
    assert job.kill("SIGKILL") is True
    assert sent[0].startswith("kill -TERM -- -4242") and sent[1].startswith("kill -KILL -- -4242")


def test_kill_rejects_an_unknown_signal_shape():
    with pytest.raises(ValueError):
        Job(_Client(), _pod(), name="j", pid=1, command="x").kill("TERM; rm -rf /")
