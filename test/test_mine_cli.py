"""Unit tests for the `lium mine` helpers that make a failed bring-up explain itself."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from lium.cli.commands import mine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_in_use_detects_a_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("0.0.0.0", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert mine._port_in_use(port) is True
    # closed again -> free
    assert mine._port_in_use(port) is False


def test_check_ports_free_names_port_and_owner(monkeypatch) -> None:
    monkeypatch.setattr(mine, "_port_in_use", lambda port: port == 8080)
    monkeypatch.setattr(mine, "_listening_process", lambda port: "python3 pid 3641")
    with pytest.raises(Exception) as exc:
        mine._check_ports_free({"service port": 8080, "SSH port": 2200})
    msg = str(exc.value)
    assert "Port 8080 (service port)" in msg
    assert "python3 pid 3641" in msg
    assert "without --auto" in msg


def test_check_ports_free_passes_when_nothing_listens(monkeypatch) -> None:
    monkeypatch.setattr(mine, "_port_in_use", lambda port: False)
    mine._check_ports_free({"service port": _free_port(), "SSH port": _free_port()})


def test_listening_process_parses_ss_output(monkeypatch) -> None:
    ss = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      5            0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=3641,fd=3))\n'
        'LISTEN 0      128          0.0.0.0:22        0.0.0.0:*    users:(("sshd",pid=1,fd=3))\n'
    )
    monkeypatch.setattr(mine, "_exists", lambda cmd: True)
    monkeypatch.setattr(mine, "_run", lambda *a, **k: (ss, ""))
    assert mine._listening_process(8080) == "python3 pid 3641"
    assert mine._listening_process(2200) == ""


def test_host_ports_from_answers_skips_public_ssh_and_blanks() -> None:
    answers = {
        "external_port": "8080",
        "ssh_port": "2200",
        "ssh_public_port": "2299",  # NAT forward on the router, not bound here
        "port_range": "",
    }
    assert mine._host_ports_from_answers(answers) == {"service port": 8080, "SSH port": 2200}
    assert mine._host_ports_from_answers({"external_port": "", "ssh_port": None}) == {}


def test_start_executor_timeout_includes_compose_diagnostics(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(cmd, check=True, capture=True, cwd=None):
        calls.append(cmd)
        if cmd.startswith("docker compose up"):
            return "", ""
        if "ps -q executor" in cmd:
            return "abc123\n", ""
        if cmd.startswith("docker inspect"):
            return "unhealthy\n", ""
        if cmd == "docker compose ps":
            return "executor-executor-runner-1  Restarting (1)\n", ""
        if cmd.startswith("docker compose logs"):
            return "failed to bind host port 0.0.0.0:8080/tcp: address already in use\n", ""
        return "", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.time, "sleep", lambda s: None)
    # Make the wait loop run exactly once, then time out.
    ticks = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr(mine.time, "time", lambda: next(ticks))

    with pytest.raises(Exception) as exc:
        mine._start_executor(tmp_path, wait_secs=5)
    msg = str(exc.value)
    assert "timed out after 5s" in msg
    assert "Restarting (1)" in msg
    assert "address already in use" in msg
    assert any(c.startswith("docker compose logs") for c in calls)


def test_provider_add_command_and_note() -> None:
    cmd = mine._provider_add_command(
        {"gpu_type": "NVIDIA RTX A6000", "gpu_count": 1}, "203.0.113.42", "8080"
    )
    assert cmd == (
        "lium provider node add --gpu-type 'NVIDIA RTX A6000' --gpu-count 1 "
        "--ip 203.0.113.42 --port 8080 --yes"
    )
    note = mine._registration_note()
    assert "opt-in" in note and "VALIDATION_PENDING" in note


class _FakePreflight:
    """Stands in for the `docker run … lium-validator` process: debug log on stderr, verdict on stdout."""

    last_cmd: str | None = None

    def __init__(self, cmd, **kwargs):
        _FakePreflight.last_cmd = cmd
        self.stderr = iter(
            [
                "2026-09-06 08:00:29,011 - __main__ - DEBUG - Starting preflight validation checks...\n",
                "2026-09-06 08:00:29,011 - __main__ - DEBUG - Running check: GPU Configuration\n",
                "2026-09-06 08:00:29,046 - __main__ - DEBUG - Running check: GPU Matrix Multiplication\n",
                "2026-09-06 08:00:45,216 - __main__ - DEBUG - Running check: VerifyX (RAM/Storage/Network)\n",
            ]
        )
        self.stdout = _Out(self.verdict)
        self.returncode = 0

    verdict = '{\n  "passed": true\n}\n'

    def wait(self):
        return self.returncode


class _Out:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_validate_executor_streams_check_names_and_reads_verdict(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePreflight)
    seen: list[str] = []

    mine._validate_executor(["--gpu-max-count", "8"], on_check=seen.append)

    assert seen == ["GPU Configuration", "GPU Matrix Multiplication", "VerifyX (RAM/Storage/Network)"]
    assert _FakePreflight.last_cmd == (
        f"docker run --rm --gpus all {mine.PREFLIGHT_IMAGE} --debug --gpu-max-count 8"
    )


def test_validate_executor_reads_the_verdict_behind_debug_noise(monkeypatch) -> None:
    import subprocess

    class Noisy(_FakePreflight):
        # --debug echoes the matrix check's stdout, including raw cipher bytes, before the verdict
        verdict = (
            "processChallengeResult secret_message:17cac6d6\nRaw cipher text:\ufffd\ufffd{\ufffdJ\n"
            "Compute Capability: 8.9\n{\n  \"passed\": true\n}\n"
        )

    monkeypatch.setattr(subprocess, "Popen", Noisy)
    mine._validate_executor()  # no exception: the verdict was found


def test_validate_executor_raises_the_image_message(monkeypatch) -> None:
    import subprocess

    class Failed(_FakePreflight):
        verdict = '{"passed": false, "message": "GPU Configuration: no GPU found"}'

    monkeypatch.setattr(subprocess, "Popen", Failed)
    with pytest.raises(Exception, match="no GPU found"):
        mine._validate_executor()


def test_validate_executor_without_verdict_shows_stderr_tail(monkeypatch) -> None:
    import subprocess

    class NoVerdict(_FakePreflight):
        verdict = ""

        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            self.stderr = iter(["docker: Error response from daemon: could not select device driver\n"])
            self.returncode = 125

    monkeypatch.setattr(subprocess, "Popen", NoVerdict)
    with pytest.raises(Exception) as exc:
        mine._validate_executor()
    assert "no verdict (exit 125)" in str(exc.value)
    assert "could not select device driver" in str(exc.value)


def test_start_preflight_pull_pulls_the_validation_image(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePreflight)
    mine._start_preflight_pull()
    assert _FakePreflight.last_cmd == f"docker pull {mine.PREFLIGHT_IMAGE}"


def test_step_message_shows_the_live_detail() -> None:
    msg = mine._StepMessage("Validating node")
    assert str(msg) == "Validating node"
    msg.detail = "GPU Matrix Multiplication"
    assert str(msg) == "Validating node (GPU Matrix Multiplication)"
