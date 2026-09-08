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
    # Loopback-only listener: the wildcard probe (what compose would bind) must
    # still report the port taken, and nothing in the test listens publicly.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert mine._port_in_use(port) is True
        assert mine._port_in_use(port, host="127.0.0.1") is True
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
        if cmd == "docker compose -f docker-compose.yml ps":
            return "executor-executor-runner-1  Up\n", ""
        if cmd == "docker compose -f docker-compose.app.yml ps":
            return "executor-executor-1  Restarting (1)\n", ""
        if cmd == "docker compose -f docker-compose.yml logs --no-color --tail 30 executor-runner":
            return "runner: starting app compose\n", ""
        if cmd == "docker compose -f docker-compose.app.yml logs --no-color --tail 30 executor":
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
    assert "runner: starting app compose" in msg
    assert "address already in use" in msg
    # `executor` exists only in docker-compose.app.yml, the runner only in the
    # default file: each is asked with its own -f, in this order.
    diagnostics = [c for c in calls if " ps" in c and "-q" not in c or " logs " in c]
    assert diagnostics == [
        "docker compose -f docker-compose.yml ps",
        "docker compose -f docker-compose.yml logs --no-color --tail 30 executor-runner",
        "docker compose -f docker-compose.app.yml ps",
        "docker compose -f docker-compose.app.yml logs --no-color --tail 30 executor",
    ]


def test_precheck_ports_skips_when_this_hosts_executor_is_already_up(monkeypatch, tmp_path: Path) -> None:
    checked: list[dict] = []
    monkeypatch.setattr(mine, "_check_ports_free", lambda ports: checked.append(ports))
    answers = {"external_port": "8080", "ssh_port": "2200"}

    # Rerun: the executor started last time owns 8080 and 2200 itself.
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ("abc123\n", "") if "ps -q executor" in cmd else ("", ""))
    assert mine._precheck_ports(tmp_path, answers) is False
    assert checked == []

    # First run: no executor container, the ports must be free.
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ("", ""))
    assert mine._precheck_ports(tmp_path, answers) is True
    assert checked == [{"service port": 8080, "SSH port": 2200}]


def test_executor_container_is_empty_when_the_directory_does_not_exist(tmp_path: Path) -> None:
    # subprocess raises FileNotFoundError for a missing cwd; that is "nothing runs from here".
    assert mine._executor_container(tmp_path / "missing" / "neurons" / "executor") == ""


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
