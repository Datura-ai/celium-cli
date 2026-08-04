"""DAH-2556: the CLI's contract with a non-interactive caller.

An autonomous agent drives the CLI through a pipe: it cannot read Rich-formatted
hints, it cannot answer a prompt, and the only two things it can act on are the
process exit code and what lands on stdout. Today a failed remote command loses
its stdout and still exits 0, `rm` rejects the `-y` an agent learned on `up`,
and an explicit `--sort` is overridden by the Pareto star, so "give me the
cheapest node" hands back the most expensive one.
"""

import json
from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.ls import display as ls_display
from lium.cli.rm import command as rm_module


def _pod(huid: str = "eager-wolf-aa", name: str = "my-pod") -> SimpleNamespace:
    return SimpleNamespace(id="pod-uuid-1", huid=huid, name=name)


def _executor(huid: str, price_per_hour: float, download: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{huid}",
        huid=huid,
        gpu_type="RTX4090",
        gpu_count=1,
        price_per_hour=price_per_hour,
        price_per_gpu=price_per_hour,
        location={"country": "Ukraine", "country_code": "UA"},
        download_speed=download,
        upload_speed=download,
        specs={"network": {"download_speed": download, "upload_speed": download}},
    )


class _FakeLium:
    """Stands in for the SDK: one pod, and an exec result the test dictates."""

    result: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def exec(self, pod, command=None, env=None, timeout=None):
        return dict(self.result)


def _run_exec(monkeypatch, result: dict, extra_args: list[str] | None = None):
    _FakeLium.result = result
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["exec", "my-pod", "echo hi", *(extra_args or [])])


def test_exec_keeps_stdout_when_the_remote_command_fails(monkeypatch):
    """The log an agent needs to diagnose a crash must survive the crash."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "VISIBLE_STDOUT\n", "stderr": ""},
    )

    assert "VISIBLE_STDOUT" in result.output


def test_exec_exits_with_the_remote_exit_code(monkeypatch):
    """`lium exec ... && next` must not run `next` after a failed command."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "", "stderr": "boom\n"},
    )

    assert result.exit_code == 3


def test_exec_exits_zero_when_the_remote_command_succeeds(monkeypatch):
    result = _run_exec(
        monkeypatch,
        {"success": True, "exit_code": 0, "stdout": "ok\n", "stderr": ""},
    )

    assert result.exit_code == 0
    assert "ok" in result.output


def test_exec_json_carries_stdout_stderr_and_exit_code(monkeypatch):
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 7, "stdout": "out\n", "stderr": "err\n"},
        ["--json"],
    )

    payload = json.loads(result.output)
    assert payload["stdout"] == "out\n"
    assert payload["stderr"] == "err\n"
    assert payload["exit_code"] == 7
    assert result.exit_code == 7


def test_exec_fails_loudly_when_no_pod_matches(monkeypatch):
    """A typo must not read as a successful run on a pod that was never touched."""
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["exec", "no-such-pod-zz", "echo hi"])

    assert result.exit_code != 0


class _FakeRmLium:
    removed: list = []

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def rm(self, pod):
        _FakeRmLium.removed.append(pod.huid)


def test_rm_accepts_yes_flag(monkeypatch):
    """Agents learn `-y` on `up`; the same idiom must not fail on teardown."""
    _FakeRmLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _FakeRmLium)

    result = CliRunner().invoke(cli, ["rm", "my-pod", "-y"])

    assert result.exit_code == 0
    assert _FakeRmLium.removed == ["eager-wolf-aa"]


def test_rm_reports_what_it_removed(monkeypatch):
    """Silence on the one mandatory step is indistinguishable from a no-op."""
    _FakeRmLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _FakeRmLium)

    result = CliRunner().invoke(cli, ["rm", "my-pod", "-y"])

    assert "eager-wolf-aa" in result.output


def test_rm_fails_loudly_when_no_pod_matches(monkeypatch):
    """`lium rm <typo>` must not look exactly like a successful termination."""
    _FakeRmLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _FakeRmLium)

    result = CliRunner().invoke(cli, ["rm", "no-such-pod-zz", "-y"])

    assert result.exit_code != 0
    assert _FakeRmLium.removed == []


def test_explicit_sort_is_not_overridden_by_the_pareto_star():
    """"Cheapest first" must mean cheapest first, star or no star."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    ordered, _ = ls_display.sort_executors(
        [cheap, expensive_but_starred], sort_by="price_total", pareto_first=False
    )

    assert ordered[0].huid == "cheap-node"


def test_price_per_hour_is_accepted_as_a_sort_key():
    """The JSON field is price_per_hour; --sort must accept the name it emits."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    pricey = _executor("pricey-node", price_per_hour=64.00, download=9999)

    ordered, _ = ls_display.sort_executors(
        [cheap, pricey], sort_by="price_per_hour", pareto_first=False
    )

    assert ordered[0].huid == "cheap-node"
