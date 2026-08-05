"""DAH-2589: `lium describe` — one manifest an agent can act on.

An agent working on a pod needs to know which external port reaches which port
inside the container, what GPU and image it got, and what the pod costs while it
runs. Today that is spread across `ps`, the dashboard and guesswork.
"""

import json

from click.testing import CliRunner

from lium.sdk import ExecutorInfo, PodInfo
from lium.cli.cli import cli
from lium.cli.describe import command as describe_module
from lium.cli.describe import display
from lium.cli.utils import EXIT_POD_NOT_FOUND


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="executor-uuid-1",
        huid="brave-otter-11",
        machine_name="NVIDIA H100 SXM 8x",
        gpu_type="H100",
        gpu_count=8,
        price_per_hour=16.0,
        price_per_gpu=2.0,
        location={"country": "US"},
        specs={"gpu": {"driver": "535.104.05", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active",
        docker_in_docker=False,
        ip="1.2.3.4",
        max_cuda_version=12.4,
        tier="secure",
    )


def _pod(ports: dict | None = None, executor: ExecutorInfo | None = None) -> PodInfo:
    return PodInfo(
        id="pod-uuid-1",
        name="my-pod",
        status="running",
        huid="eager-wolf-aa",
        ssh_cmd="ssh root@1.2.3.4 -p 34567",
        ports={"22": 34567, "8000": 34568} if ports is None else ports,
        created_at="2026-08-05T00:00:00Z",
        updated_at="2026-08-05T00:00:00Z",
        executor=_executor() if executor is None else executor,
        template={"name": "PyTorch 2.4", "docker_image": "pytorch/pytorch", "docker_image_tag": "2.4"},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


class _FakeLium:
    """Stands in for the SDK: whatever list of pods the test dictates."""

    pods: list = []

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)


def _run_describe(monkeypatch, pods: list, target: str = "my-pod") -> tuple:
    _FakeLium.pods = pods
    monkeypatch.setattr(describe_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["describe", target, "--json"])


def test_manifest_names_the_port_direction():
    """Mapping keys are container ports — the manifest says so instead of leaving it to guesswork."""
    manifest = display.build_manifest(_pod())

    assert manifest["ports"]["direction"] == "internal -> external"
    assert manifest["ports"]["mapping"] == {"22": 34567, "8000": 34568}


def test_manifest_singles_out_the_ssh_port():
    """The external port reaching container port 22 is the one an agent needs first."""
    manifest = display.build_manifest(_pod())

    assert manifest["ports"]["ssh_external"] == 34567


def test_manifest_service_ports_exclude_ssh():
    """Everything except 22 is a port a service can be published on."""
    manifest = display.build_manifest(_pod(ports={"22": 34567, "8000": 34568, "8888": 34569}))

    assert manifest["ports"]["service_ports"] == [
        {"internal": 8000, "external": 34568},
        {"internal": 8888, "external": 34569},
    ]


def test_manifest_reports_gpu_from_executor_specs():
    """GPU model and driver come from the executor specs, not from the machine name."""
    manifest = display.build_manifest(_pod())

    assert manifest["gpu"]["model"] == "H100 80GB HBM3"
    assert manifest["gpu"]["driver_version"] == "535.104.05"
    assert manifest["gpu"]["count"] == 8


def test_manifest_without_executor_still_describes_the_pod():
    """A pod whose executor the API omitted must not take the whole manifest down."""
    pod = _pod()
    pod.executor = None

    manifest = display.build_manifest(pod)

    assert manifest["gpu"] is None
    assert manifest["machine"] is None
    assert manifest["ports"]["ssh_external"] == 34567
    assert manifest["billing"]["price_per_hour"] is None


def test_manifest_bills_by_uptime():
    """Spend is uptime × price, so an agent can see what the pod has cost so far."""
    manifest = display.build_manifest(_pod())

    assert manifest["billing"]["price_per_hour"] == 16.0
    assert manifest["billing"]["spent_usd"] > 0
    assert manifest["pod"]["uptime_hours"] > 0


def test_describe_json_prints_the_manifest_on_stdout(monkeypatch):
    """`--json` output has to parse as-is — no spinner, no Rich decoration."""
    result = _run_describe(monkeypatch, [_pod()])

    assert result.exit_code == 0
    manifest = json.loads(result.stdout)
    assert manifest["pod"]["huid"] == "eager-wolf-aa"
    assert manifest["ports"]["ssh_external"] == 34567


def test_describe_resolves_a_pod_by_huid(monkeypatch):
    """The huid printed by `ps` is a valid handle for describe."""
    result = _run_describe(monkeypatch, [_pod()], target="eager-wolf-aa")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["pod"]["id"] == "pod-uuid-1"


def test_describe_unknown_pod_exits_with_pod_not_found(monkeypatch):
    """A typo must fail with the pod-not-found code, not an empty success."""
    result = _run_describe(monkeypatch, [_pod()], target="no-such-pod-zz")

    assert result.exit_code == EXIT_POD_NOT_FOUND
