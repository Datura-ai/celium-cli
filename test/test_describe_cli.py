"""DAH-2589: `lium describe` — one manifest an agent can act on.

An agent working on a pod needs to know which external port reaches which port
inside the container, what GPU and image it got, and what the pod costs while it
runs. Today that is spread across `ps`, the dashboard and guesswork.
"""

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from lium.sdk import ExecutorInfo, PodInfo
from lium.sdk.exceptions import LiumNotFoundError
from lium.cli.cli import cli
from lium.cli.describe import command as describe_module
from lium.cli.describe import display
from lium.cli.utils import EXIT_GENERAL_ERROR, EXIT_POD_NOT_FOUND

_DEFAULT = object()
PRICE_PER_HOUR = 16.0
UPTIME_HOURS = 2


def _hours_ago(hours: int) -> str:
    """Creation timestamp relative to now, so the test does not depend on the wall clock."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="executor-uuid-1",
        huid="brave-otter-11",
        machine_name="NVIDIA H100 SXM 8x",
        gpu_type="H100",
        gpu_count=8,
        price_per_hour=PRICE_PER_HOUR,
        price_per_gpu=2.0,
        location={"country": "US"},
        specs={"gpu": {"driver": "535.104.05", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active",
        docker_in_docker=False,
        ip="1.2.3.4",
        max_cuda_version=12.4,
        tier="secure",
    )


def _pod(ports=_DEFAULT, executor=_DEFAULT, template=_DEFAULT) -> PodInfo:
    return PodInfo(
        id="pod-uuid-1",
        name="my-pod",
        status="running",
        huid="eager-wolf-aa",
        ssh_cmd="ssh root@1.2.3.4 -p 34567",
        ports={"22": 34567, "8000": 34568} if ports is _DEFAULT else ports,
        created_at=_hours_ago(UPTIME_HOURS),
        updated_at=_hours_ago(0),
        executor=_executor() if executor is _DEFAULT else executor,
        template=(
            {"name": "PyTorch 2.4", "docker_image": "pytorch/pytorch", "docker_image_tag": "2.4"}
            if template is _DEFAULT else template
        ),
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


class _FakeLium:
    """Stands in for the SDK: the pods a test dictates, or the error it dictates."""

    pods: list = []
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        if self.error:
            raise self.error
        return list(self.pods)


def _run_describe(monkeypatch, pods: list, target: str = "my-pod", error: Exception | None = None):
    _FakeLium.pods = pods
    _FakeLium.error = error
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


def test_manifest_finds_the_ssh_port_under_an_integer_key():
    """A mapping built in Python carries int keys; the SSH port must not vanish."""
    manifest = display.build_manifest(_pod(ports={22: 34567, 8000: 34568}))

    assert manifest["ports"]["ssh_external"] == 34567
    assert manifest["ports"]["service_ports"] == [{"internal": 8000, "external": 34568}]


def test_manifest_keeps_a_non_numeric_port_key_instead_of_crashing():
    """A key like "8000/tcp" is reported as-is rather than taking the command down."""
    manifest = display.build_manifest(_pod(ports={"22": 34567, "8000/tcp": 34568}))

    assert manifest["ports"]["service_ports"] == [{"internal": "8000/tcp", "external": 34568}]


def test_manifest_service_ports_exclude_ssh():
    """Everything except 22 is a port a service can be published on."""
    manifest = display.build_manifest(_pod(ports={"22": 34567, "8000": 34568, "8888": 34569}))

    assert manifest["ports"]["service_ports"] == [
        {"internal": 8000, "external": 34568},
        {"internal": 8888, "external": 34569},
    ]


def test_manifest_survives_a_pod_without_ports():
    """The backend sends ports_mapping: null before ports are allocated."""
    manifest = display.build_manifest(_pod(ports=None))

    assert manifest["ports"]["mapping"] == {}
    assert manifest["ports"]["ssh_external"] is None
    assert manifest["ports"]["service_ports"] == []


def test_manifest_reports_gpu_from_executor_specs():
    """GPU model and driver come from the executor specs, not from the machine name."""
    manifest = display.build_manifest(_pod())

    assert manifest["gpu"]["model"] == "H100 80GB HBM3"
    assert manifest["gpu"]["driver_version"] == "535.104.05"
    assert manifest["gpu"]["count"] == 8


def test_manifest_without_executor_still_describes_the_pod():
    """A pod whose executor the API omitted must not take the whole manifest down."""
    manifest = display.build_manifest(_pod(executor=None))

    assert manifest["gpu"] is None
    assert manifest["machine"] is None
    assert manifest["ports"]["ssh_external"] == 34567
    assert manifest["billing"]["price_per_hour"] is None


def test_manifest_bills_uptime_times_price():
    """Spend is uptime × price, so an agent can see what the pod has cost so far."""
    manifest = display.build_manifest(_pod())

    assert abs(manifest["pod"]["uptime_hours"] - UPTIME_HOURS) < 0.01
    assert abs(manifest["billing"]["spent_usd"] - UPTIME_HOURS * PRICE_PER_HOUR) < 0.1


def test_table_renders_a_pod_the_api_barely_described():
    """The human path must not crash where the JSON path degrades quietly."""
    bare_pod = _pod(ports=None, executor=None, template=None)
    bare_pod.status = None
    bare_pod.ssh_cmd = None
    bare_pod.created_at = ""

    table = display.build_manifest_table(display.build_manifest(bare_pod))

    assert table.row_count > 0


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
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "pod_not_found"


def test_describe_does_not_blame_the_pod_id_for_an_api_failure(monkeypatch):
    """The SDK's own 404 text contains "not found" — that must not read as a bad pod id."""
    result = _run_describe(
        monkeypatch, [], error=LiumNotFoundError("Resource not found: /pods")
    )

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert json.loads(result.stderr)["error"]["code"] != "pod_not_found"
