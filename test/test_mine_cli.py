from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import mine
from lium.cli.utils import CliFailure


@contextmanager
def _no_spinner(*args, **kwargs):
    yield


def _make_executor_dir(tmp_path: Path) -> Path:
    executor_dir = tmp_path / "compute-subnet" / "neurons" / "executor"
    executor_dir.mkdir(parents=True)
    return executor_dir


def test_verify_sysbox_rejects_missing_executable(monkeypatch, tmp_path):
    executor_dir = _make_executor_dir(tmp_path)
    monkeypatch.setattr(mine, "_exists", lambda command: False)

    with pytest.raises(CliFailure) as exc_info:
        mine._verify_sysbox(executor_dir)

    error = exc_info.value
    assert error.code == "sysbox_unavailable"
    assert "sysbox-runc executable is not installed" in error.message
    assert f"sudo bash {executor_dir}/nvidia_docker_sysbox_setup.sh" in error.message
    assert mine.SYSBOX_DOCS_URL in error.message


def test_verify_sysbox_rejects_unregistered_runtime(monkeypatch, tmp_path):
    executor_dir = _make_executor_dir(tmp_path)
    commands = []

    monkeypatch.setattr(mine, "_exists", lambda command: True)

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        return '{"runc": {"path": "runc"}}', ""

    monkeypatch.setattr(mine, "_run", fake_run)

    with pytest.raises(CliFailure) as exc_info:
        mine._verify_sysbox(executor_dir)

    assert (
        "active Docker daemon does not have the sysbox-runc runtime registered"
        in exc_info.value.message
    )
    assert "same Docker daemon" in exc_info.value.message
    assert commands == ["docker info --format '{{json .Runtimes}}'"]


def test_verify_sysbox_reports_runtime_inspection_failure(monkeypatch, tmp_path):
    executor_dir = _make_executor_dir(tmp_path)
    monkeypatch.setattr(mine, "_exists", lambda command: True)
    monkeypatch.setattr(
        mine,
        "_run",
        lambda command: (_ for _ in ()).throw(
            RuntimeError("permission denied connecting to Docker")
        ),
    )

    with pytest.raises(CliFailure) as exc_info:
        mine._verify_sysbox(executor_dir)

    assert "runtime registry could not be inspected" in exc_info.value.message
    assert "permission denied connecting to Docker" in exc_info.value.message
    assert "nvidia_docker_sysbox_setup.sh" in exc_info.value.message


def test_verify_sysbox_reports_failed_gpu_smoke_test(monkeypatch, tmp_path):
    executor_dir = _make_executor_dir(tmp_path)
    monkeypatch.setattr(mine, "_exists", lambda command: True)

    def fake_run(command, *args, **kwargs):
        if command.startswith("docker info"):
            return '{"runc": {}, "sysbox-runc": {}}', ""
        raise RuntimeError('namespace {"time" ""} does not exist')

    monkeypatch.setattr(mine, "_run", fake_run)

    with pytest.raises(CliFailure) as exc_info:
        mine._verify_sysbox(executor_dir)

    assert (
        "Sysbox is registered, but its GPU verification failed"
        in exc_info.value.message
    )
    assert 'namespace {"time" ""} does not exist' in exc_info.value.message
    assert "nvidia_docker_sysbox_setup.sh" in exc_info.value.message


def test_verify_sysbox_runs_validator_equivalent_gpu_check(monkeypatch, tmp_path):
    executor_dir = _make_executor_dir(tmp_path)
    commands = []
    monkeypatch.setattr(mine, "_exists", lambda command: True)

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        if command.startswith("docker info"):
            return '{"runc": {}, "sysbox-runc": {}}', ""
        return "NVIDIA H100", ""

    monkeypatch.setattr(mine, "_run", fake_run)

    mine._verify_sysbox(executor_dir)

    assert commands == [
        "docker info --format '{{json .Runtimes}}'",
        "docker run --rm --runtime=sysbox-runc --gpus all "
        "daturaai/compute-subnet-executor:latest nvidia-smi",
    ]


def test_mine_stops_before_configuration_and_start_when_sysbox_is_missing(
    monkeypatch,
    tmp_path,
):
    executor_dir = _make_executor_dir(tmp_path)
    configured = False
    started = False

    monkeypatch.setattr(mine, "timed_step_status", _no_spinner)
    monkeypatch.setattr(
        mine,
        "_gather_inputs",
        lambda hotkey, auto: {
            "hotkey": "5F4hQyQ1wY2M6zS4mP7dN8kR2tV9xB3cL6jH1nT5uA7q",
            "internal_port": "8080",
            "external_port": "8080",
            "ssh_port": "2200",
            "ssh_public_port": "",
            "port_range": "",
        },
    )
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *args, **kwargs: None)
    monkeypatch.setattr(mine, "_install_executor_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(mine, "_check_prereqs", lambda: None)
    monkeypatch.setattr(
        mine,
        "_verify_sysbox",
        lambda path: (_ for _ in ()).throw(
            mine._sysbox_failure("The sysbox-runc executable is not installed.", path)
        ),
    )

    def configure(*args, **kwargs):
        nonlocal configured
        configured = True

    def start(*args, **kwargs):
        nonlocal started
        started = True

    monkeypatch.setattr(mine, "_setup_executor_env", configure)
    monkeypatch.setattr(mine, "_start_executor", start)

    result = CliRunner().invoke(
        cli,
        ["mine", "--auto", "--dir", str(executor_dir.parents[1])],
    )

    assert result.exit_code == 1
    assert "sysbox-runc executable is not installed" in result.output
    assert "nvidia_docker_sysbox_setup.sh" in result.output.replace("\n", "")
    assert configured is False
    assert started is False


def test_mine_returns_nonzero_for_other_setup_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(mine, "timed_step_status", _no_spinner)
    monkeypatch.setattr(
        mine,
        "_gather_inputs",
        lambda hotkey, auto: {
            "hotkey": "hotkey",
            "internal_port": "8080",
            "external_port": "8080",
            "ssh_port": "2200",
            "ssh_public_port": "",
            "port_range": "",
        },
    )
    monkeypatch.setattr(
        mine,
        "_clone_or_update_repo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("repository unavailable")
        ),
    )

    result = CliRunner().invoke(
        cli,
        ["mine", "--auto", "--dir", str(tmp_path / "compute-subnet")],
    )

    assert result.exit_code == 1
    assert "repository unavailable" in result.output
