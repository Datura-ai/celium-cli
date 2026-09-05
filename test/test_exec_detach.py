"""`lium exec --detach`: start a long job on a pod and come back at once.

Plain `exec` holds the SSH session open until every child of the command has
exited, so a training run started with `nohup ... &` still blocks the caller
unless it also uses `setsid` and closes stdin. Callers ended up wrapping every
command by hand; the CLI can do it once, correctly.
"""

import base64
import json
import shlex
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.commands.exec import (
    DetachedExecution,
    build_detached_command,
    build_detached_script_command,
    default_detach_log_path,
    detach_timestamp,
    parse_detached_pid,
)
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR


def _pod(huid: str = "eager-wolf-aa", name: str = "my-pod") -> SimpleNamespace:
    return SimpleNamespace(id=f"pod-{huid}", huid=huid, name=name)


class _FakeLium:
    """Records the command lines sent to each pod and answers with a PID."""

    pods: list = []
    stdout: str = "4242\n"
    stderr: str = ""
    exit_code: int = 0
    sent: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def exec(self, pod, command=None, env=None):
        _FakeLium.sent.append((pod.huid, command))
        return {
            "success": self.exit_code == 0,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def exec_all(self, pods, command=None, env=None):
        return [self.exec(pod, command=command, env=env) for pod in pods]


@pytest.fixture
def fake_lium(monkeypatch):
    _FakeLium.pods = [_pod()]
    _FakeLium.stdout = "4242\n"
    _FakeLium.stderr = ""
    _FakeLium.exit_code = 0
    _FakeLium.sent = []
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)
    monkeypatch.setattr(exec_module, "detach_timestamp", lambda now=None: "20260101-120000")
    return _FakeLium


def _run(args):
    return CliRunner().invoke(cli, ["exec", *args])


def test_detached_command_uses_nohup_setsid_and_closes_stdin():
    remote = build_detached_command("python train.py --epochs 3", "/workspace/logs/run.log")

    assert remote.startswith("mkdir -p /workspace/logs || exit 1; ")
    assert "nohup setsid bash -lc 'python train.py --epochs 3'" in remote
    assert "> /workspace/logs/run.log 2>&1 < /dev/null & echo $!" in remote


def test_detached_command_quotes_the_users_command_as_one_argument():
    """Quotes inside the command must survive the trip through bash -lc."""
    command = "echo \"it's\" done; sleep 1"

    remote = build_detached_command(command, "/workspace/logs/run.log")

    assert f"bash -lc {shlex.quote(command)}" in remote


def test_detached_command_quotes_the_log_path():
    remote = build_detached_command("true", "/workspace/my logs/run.log")

    assert "mkdir -p '/workspace/my logs'" in remote
    assert "> '/workspace/my logs/run.log'" in remote


def test_default_log_path_is_under_workspace_logs():
    stamp = detach_timestamp(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    assert default_detach_log_path(stamp) == "/workspace/logs/exec-20260101-120000.log"


def test_detached_script_is_copied_before_it_is_started():
    """The script must exist on the pod as a file before anything runs from it."""
    script = "#!/bin/bash\necho 'hello' \"world\" $1\n"

    remote = build_detached_script_command(script, "/workspace/logs/run.log", "20260101-120000")

    encoded = base64.b64encode(script.encode()).decode()
    upload = f"printf %s {encoded} | base64 -d > /tmp/lium-exec-20260101-120000.sh"
    assert remote.startswith(upload)
    assert remote.index("chmod +x /tmp/lium-exec-20260101-120000.sh") < remote.index("nohup")
    assert "bash -lc /tmp/lium-exec-20260101-120000.sh" in remote


@pytest.mark.parametrize(
    "stdout, expected",
    [("4242\n", 4242), ("nohup: ignoring input\n4242\n", 4242), ("", None), ("bash: setsid: not found\n", None)],
)
def test_parse_detached_pid(stdout, expected):
    assert parse_detached_pid(stdout) == expected


def test_detach_prints_pid_and_log_and_exits_zero(fake_lium):
    result = _run(["my-pod", "-d", "python train.py"])

    assert result.exit_code == 0, result.output
    assert "PID 4242" in result.output
    assert "/workspace/logs/exec-20260101-120000.log" in result.output
    [(huid, remote)] = fake_lium.sent
    assert huid == "eager-wolf-aa"
    assert remote == build_detached_command("python train.py", "/workspace/logs/exec-20260101-120000.log")


def test_detach_honours_an_explicit_log_path(fake_lium):
    result = _run(["my-pod", "--detach", "--log", "/workspace/train.log", "python train.py"])

    assert result.exit_code == 0, result.output
    assert "/workspace/train.log" in result.output
    [(_, remote)] = fake_lium.sent
    assert "> /workspace/train.log 2>&1" in remote
    assert "mkdir -p /workspace" in remote


def test_detach_json_carries_pid_and_log(fake_lium):
    result = _run(["my-pod", "-d", "python train.py", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "results": [{
            "pod": "eager-wolf-aa",
            "pid": 4242,
            "log": "/workspace/logs/exec-20260101-120000.log",
            "error": None,
        }],
    }


def test_detach_with_script_uploads_then_starts_it(fake_lium, tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\necho from-script\n")

    result = _run(["my-pod", "-d", "--script", str(script)])

    assert result.exit_code == 0, result.output
    [(_, remote)] = fake_lium.sent
    assert remote == build_detached_script_command(
        "#!/bin/bash\necho from-script\n", "/workspace/logs/exec-20260101-120000.log", "20260101-120000"
    )


def test_detach_fails_when_no_pid_came_back(fake_lium):
    """A launcher that printed nothing did not start the job; that is not success."""
    fake_lium.stdout = ""
    fake_lium.stderr = "bash: setsid: command not found\n"
    fake_lium.exit_code = 127

    result = _run(["my-pod", "-d", "python train.py"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "setsid: command not found" in result.output


def test_detach_json_reports_a_failed_start(fake_lium):
    fake_lium.stdout = ""
    fake_lium.exit_code = 1

    result = _run(["my-pod", "-d", "python train.py", "--json"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["results"][0]["pid"] is None
    assert payload["results"][0]["error"]


def test_detach_on_several_pods_starts_on_each(fake_lium):
    fake_lium.pods = [_pod("eager-wolf-aa", "a"), _pod("brave-fox-3a", "b")]

    result = _run(["all", "-d", "python train.py"])

    assert result.exit_code == 0, result.output
    assert [huid for huid, _ in fake_lium.sent] == ["eager-wolf-aa", "brave-fox-3a"]
    assert result.output.count("PID 4242") == 2


def test_log_without_detach_is_rejected(fake_lium):
    result = _run(["my-pod", "--log", "/workspace/x.log", "echo hi"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert fake_lium.sent == []


def test_plain_exec_is_unchanged(fake_lium):
    """Without --detach the command goes to the pod exactly as typed."""
    fake_lium.stdout = "ok\n"

    result = _run(["my-pod", "echo hi"])

    assert result.exit_code == 0, result.output
    assert fake_lium.sent == [("eager-wolf-aa", "echo hi")]


def test_detached_execution_uses_stderr_as_the_error_when_there_is_no_pid():
    execution = DetachedExecution.from_sdk_result(
        _pod(), {"stdout": "", "stderr": "boom\n", "exit_code": 1}, "/workspace/logs/x.log"
    )

    assert execution.succeeded is False
    assert execution.error == "boom"
