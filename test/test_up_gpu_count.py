"""`lium up` must not hand back a pod with fewer GPUs than were requested or billed.

Two failure modes were observed on real rentals: a pod comes up RUNNING with a
smaller GPU count than ``--count`` asked for, and a pod is billed for N GPUs
while ``nvidia-smi`` inside it sees fewer. Both used to exit 0 and connect.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.up import command as up_module
from lium.cli.up.actions import (
    VerifyGpuCountAction,
    billed_gpu_count,
    parse_visible_gpu_count,
)
from lium.cli.utils import EXIT_GENERAL_ERROR

NODE_ID = "id-brave-orbit-b9"


def _executor(gpu_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=NODE_ID,
        huid="brave-orbit-b9",
        gpu_type="H200",
        gpu_count=gpu_count,
        price_per_hour=8.0 * gpu_count,
        price_per_gpu=8.0,
        location={"country": "United States", "country_code": "US"},
        specs={"gpu": {"count": gpu_count}},
        download_speed=1000,
        upload_speed=1000,
        available_port_count=5,
        docker_in_docker=False,
        max_cuda_version=12.8,
        tier="secure",
    )


def _pod(billed_gpus: int | None) -> SimpleNamespace:
    """A RUNNING pod. ``None`` stands for an API payload without a GPU count."""
    executor = _executor(billed_gpus if billed_gpus is not None else 1)
    if billed_gpus is None:
        executor.specs = {}
    return SimpleNamespace(
        id="pod-uuid-1234",
        huid="eager-wolf-aa",
        name="brave-orbit-b9",
        status="RUNNING",
        ssh_cmd="ssh root@203.0.113.10 -p 20022",
        ports={"22": 20022},
        executor=executor,
    )


class _FakeLium:
    """Stands in for the SDK: a node, the pod it produced, and what nvidia-smi says."""

    node_gpus = 8
    billed_gpus: int | None = 8
    visible_gpus: int | None = 8
    exec_error: Exception | None = None
    removed: list[str] = []
    exec_commands: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def get_executor(self, executor_id):
        return _executor(self.node_gpus)

    def ls(self, **kwargs):
        return [_executor(self.node_gpus)]

    def default_docker_template(self, executor_id):
        return SimpleNamespace(id="tpl-1", name="pytorch")

    def get_deployment_estimate(self, executor_id, template_id):
        return {}

    def up(self, **kwargs):
        return {"id": "pod-uuid-1234", "name": "brave-orbit-b9"}

    def ps(self):
        return [_pod(self.billed_gpus)]

    def exec(self, pod, command=None, env=None):
        _FakeLium.exec_commands.append(command)
        if self.exec_error:
            raise self.exec_error
        stdout = "" if self.visible_gpus is None else f"{self.visible_gpus}\n"
        return {"success": True, "exit_code": 0, "stdout": stdout, "stderr": ""}

    def rm(self, pod):
        _FakeLium.removed.append(pod.huid)


def _run_up(monkeypatch, *args, node_gpus=8, billed=8, visible=8, exec_error=None):
    """Run `up`. `-c` cannot be combined with a node id, so it selects by --gpu filter."""
    _FakeLium.node_gpus = node_gpus
    _FakeLium.billed_gpus = billed
    _FakeLium.visible_gpus = visible
    _FakeLium.exec_error = exec_error
    _FakeLium.removed = []
    _FakeLium.exec_commands = []
    monkeypatch.setattr(up_module, "Lium", _FakeLium)
    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: [])
    target = ["--gpu", "H200"] if "-c" in args else ["some-node-id"]
    return CliRunner().invoke(cli, ["up", *target, "-y", "--no-ssh", *args])


def test_up_succeeds_when_the_pod_has_the_requested_gpus(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", node_gpus=8, billed=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.removed == []


def test_up_fails_on_a_silent_gpu_downgrade(monkeypatch):
    """Requested 8, billed 1: the pod is named, both numbers are shown, exit is non-zero."""
    result = _run_up(monkeypatch, "-c", "8", node_gpus=8, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 8" in result.output
    assert "billed for 1" in result.output
    assert NODE_ID in result.output
    assert "eager-wolf-aa" in result.output
    # Without --strict-gpus the pod is left for the caller to inspect or remove.
    assert _FakeLium.removed == []


def test_up_without_count_compares_against_the_chosen_node(monkeypatch):
    """No --count: the node the user chose (4 GPUs) is the request."""
    result = _run_up(monkeypatch, node_gpus=4, billed=2)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "requested 4" in result.output
    assert "billed for 2" in result.output


def test_up_does_not_run_nvidia_smi_without_verify_gpus(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", billed=8, visible=2)

    assert result.exit_code == 0, result.output
    assert _FakeLium.exec_commands == []


def test_verify_gpus_fails_on_a_phantom_gpu(monkeypatch):
    """Billed 4, nvidia-smi sees 2: the pod is not what is being paid for."""
    result = _run_up(monkeypatch, "-c", "4", "--verify-gpus", node_gpus=4, billed=4, visible=2)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "billed for 4" in result.output
    assert "nvidia-smi reports 2" in result.output
    assert NODE_ID in result.output
    assert _FakeLium.exec_commands == ["nvidia-smi -L | wc -l"]
    assert _FakeLium.removed == []


def test_verify_gpus_passes_when_nvidia_smi_agrees(monkeypatch):
    result = _run_up(monkeypatch, "-c", "4", "--verify-gpus", node_gpus=4, billed=4, visible=4)

    assert result.exit_code == 0, result.output


def test_strict_gpus_removes_a_downgraded_pod(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", "--strict-gpus", node_gpus=8, billed=1)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert _FakeLium.removed == ["eager-wolf-aa"]
    assert "removed" in result.output


def test_strict_gpus_removes_a_pod_with_phantom_gpus(monkeypatch):
    result = _run_up(
        monkeypatch, "-c", "4", "--verify-gpus", "--strict-gpus", node_gpus=4, billed=4, visible=2
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert _FakeLium.removed == ["eager-wolf-aa"]


def test_strict_gpus_keeps_a_pod_it_could_not_check(monkeypatch):
    """An SSH failure is not evidence of a bad pod; strict mode must not remove it."""
    result = _run_up(
        monkeypatch,
        "-c", "4", "--verify-gpus", "--strict-gpus",
        node_gpus=4, billed=4, exec_error=RuntimeError("connection refused"),
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Could not verify GPU count" in result.output
    assert _FakeLium.removed == []


def test_strict_gpus_keeps_a_matching_pod(monkeypatch):
    result = _run_up(monkeypatch, "-c", "8", "--strict-gpus", "--verify-gpus", billed=8, visible=8)

    assert result.exit_code == 0, result.output
    assert _FakeLium.removed == []


def test_billed_count_is_unknown_when_the_api_sent_none():
    """ExecutorInfo defaults gpu_count to 1; that default must not read as a downgrade."""
    assert billed_gpu_count(_pod(None)) is None
    assert billed_gpu_count(_pod(8)) == 8
    assert billed_gpu_count(SimpleNamespace(executor=None)) is None


def test_unknown_billed_count_does_not_fail_the_pod():
    result = VerifyGpuCountAction().execute({
        "lium": None, "pod": _pod(None), "expected_count": 8, "executor_id": NODE_ID,
    })

    assert result.ok is True
    assert result.data["billed"] is None


@pytest.mark.parametrize(
    "stdout, expected",
    [("8\n", 8), ("       2\n", 2), ("", None), ("bash: nvidia-smi: command not found\n", None)],
)
def test_parse_visible_gpu_count(stdout, expected):
    assert parse_visible_gpu_count(stdout) == expected


def test_verify_reports_an_unparseable_nvidia_smi_result_without_a_mismatch():
    """No number is "could not check", not "wrong count" — strict mode must not remove."""
    class _NoSmi:
        def exec(self, pod, command=None, env=None):
            return {"stdout": "nvidia-smi: command not found\n", "exit_code": 127}

    result = VerifyGpuCountAction().execute({
        "lium": _NoSmi(), "pod": _pod(4), "expected_count": 4,
        "executor_id": NODE_ID, "verify_via_ssh": True,
    })

    assert result.ok is False
    assert result.data["mismatch"] is False
