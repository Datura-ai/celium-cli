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

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display as ls_display
from lium.cli.rm import command as rm_module
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND


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
        docker_in_docker=False,
        max_cuda_version=12.4,
        tier="secure",
        # Optional ExecutorInfo fields the ls table reads. They default to None on the
        # real model, so the stand-in has to carry them or the table raises.
        available_gpu_count=1,
        min_gpu_count_for_rental=None,
        reliability_score=None,
        uptime_in_minutes=None,
    )


class _FakeLium:
    """Stands in for the SDK: one pod, and an exec result the test dictates."""

    result: dict[str, object] = {}

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def exec(self, pod, command=None, env=None):
        return dict(self.result)


def _run_exec(
    monkeypatch,
    result: dict[str, object],
    extra_args: list[str] | None = None,
    target: str = "my-pod",
):
    _FakeLium.result = result
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["exec", target, "echo hi", *(extra_args or [])])


def _run_rm(monkeypatch, target: str = "my-pod"):
    _FakeRmLium.removed = []
    monkeypatch.setattr(rm_module, "Lium", _FakeRmLium)
    return CliRunner().invoke(cli, ["rm", target, "-y"])


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
    assert payload["ok"] is False
    assert payload["results"] == [
        {"pod": "eager-wolf-aa", "stdout": "out\n", "stderr": "err\n", "exit_code": 7, "error": None}
    ]
    assert result.exit_code == 7


def test_exec_fails_loudly_when_no_pod_matches(monkeypatch):
    """A typo must not read as a successful run on a pod that was never touched."""
    result = _run_exec(
        monkeypatch,
        {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
        target="no-such-pod-zz",
    )

    assert result.exit_code == EXIT_POD_NOT_FOUND


class _FakeRmLium:
    removed: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return [_pod()]

    def rm(self, pod):
        _FakeRmLium.removed.append(pod.huid)


def test_rm_accepts_yes_and_reports_what_it_removed(monkeypatch):
    """Agents learn `-y` on `up`, and silence on teardown reads like a no-op."""
    result = _run_rm(monkeypatch)

    assert result.exit_code == 0
    assert _FakeRmLium.removed == ["eager-wolf-aa"]
    assert "eager-wolf-aa" in result.output


def test_rm_fails_loudly_when_no_pod_matches(monkeypatch):
    """`lium rm <typo>` must not look exactly like a successful termination."""
    result = _run_rm(monkeypatch, target="no-such-pod-zz")

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert _FakeRmLium.removed == []


@pytest.mark.parametrize("sort_by", ["price_total", "price_per_hour"])
def test_explicit_sort_is_not_overridden_by_the_pareto_star(sort_by):
    """"Cheapest first" must mean cheapest first, star or no star."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    ordered, _ = ls_display.sort_executors([cheap, expensive_but_starred], sort_by=sort_by)

    assert ordered[0].huid == "cheap-node"


@pytest.mark.parametrize("sort_by", ["price_total", "price_per_hour"])
def test_cheapest_sort_survives_the_whole_ls_command(monkeypatch, sort_by):
    """End to end: the option a caller types must reach the sort it names."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    class _FakeLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            return [expensive_but_starred, cheap]

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLsLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--sort", sort_by, "--format", "json"])

    assert result.exit_code == 0, result.output
    assert [row["huid"] for row in json.loads(result.output)][0] == "cheap-node"


def test_default_ls_ordering_still_puts_the_starred_node_first(monkeypatch):
    """Without --sort the ★ ordering a human reads is unchanged."""
    cheap = _executor("cheap-node", price_per_hour=0.30, download=10)
    expensive_but_starred = _executor("starred-node", price_per_hour=64.00, download=9999)

    class _FakeLsLium:
        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            return [cheap, expensive_but_starred]

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLsLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["huid"] == "starred-node"


def test_exec_script_flag_reads_the_file(monkeypatch, tmp_path):
    """--script is the documented way to send a multi-line job; it must work."""
    script = tmp_path / "job.sh"
    script.write_text("echo from-script\n")
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "ran\n", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "--script", str(script)])

    assert result.exit_code == 0, result.output


def test_exec_config_errors_stay_json_under_json(monkeypatch):
    """A machine caller must get the envelope on every failure, not just some."""
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "-e", "NOT_A_PAIR", "cmd", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.stderr)["ok"] is False


def test_exec_stderr_carries_no_rich_markup(monkeypatch):
    """The command's own stderr must reach the caller unstyled."""
    result = _run_exec(
        monkeypatch,
        {"success": False, "exit_code": 3, "stdout": "", "stderr": "boom\n"},
    )

    assert "boom" in result.output
    assert "[/]" not in result.output


def test_rm_named_pod_on_an_empty_account_fails(monkeypatch):
    """A typo must fail even when the account happens to hold no pods."""

    class _EmptyLium:
        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(rm_module, "Lium", _EmptyLium)

    result = CliRunner().invoke(cli, ["rm", "no-such-pod-zz", "-y"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_rm_all_on_an_empty_account_is_a_no_op(monkeypatch):
    """Removing everything when there is nothing is success, not failure."""

    class _EmptyLium:
        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(rm_module, "Lium", _EmptyLium)

    result = CliRunner().invoke(cli, ["rm", "--all", "-y"])

    assert result.exit_code == 0


def test_exec_missing_script_still_speaks_json(monkeypatch):
    """A bad --script path must not fall out as click usage text under --json."""
    _FakeLium.result = {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(exec_module, "Lium", _FakeLium)

    result = CliRunner().invoke(
        cli, ["exec", "my-pod", "--script", "/no/such/file.sh", "--json"]
    )

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "unreadable_script"


def test_exec_json_keeps_stdout_clean_when_the_api_fails(monkeypatch):
    """stdout belongs to the JSON consumer; the progress spinner must stay off it."""

    class _BrokenLium:
        def __init__(self, *args, **kwargs):
            pass

        def ps(self):
            raise RuntimeError("api down")

    monkeypatch.setattr(exec_module, "Lium", _BrokenLium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "echo hi", "--json"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert json.loads(result.stderr)["ok"] is False


def test_up_fails_when_ssh_is_unavailable(monkeypatch):
    """A pod that is rented but unreachable must not report success — DAH-2556."""
    from lium.cli.up import actions as up_actions
    from lium.cli.utils import CliFailure, EXIT_SSH_ERROR

    def _no_ssh(pod_name):
        raise CliFailure(
            "ssh_unavailable",
            f"No SSH connection available for pod '{pod_name}'",
            EXIT_SSH_ERROR,
        )

    monkeypatch.setattr("lium.cli.ssh.command.get_ssh_method_and_pod", _no_ssh)

    result = up_actions.PrepareSSHAction().execute({"pod_name": "brave-orbit-b9"})

    assert result.ok is False
    assert "brave-orbit-b9" in result.error


@pytest.mark.parametrize(
    "returncode, connected", [(0, True), (3, True), (255, False)]
)
def test_ssh_session_connected_reports_only_connection_failure(monkeypatch, returncode, connected):
    """A remote shell exiting non-zero is the user's business; 255 is ours."""
    from lium.cli.ssh import command as ssh_module

    monkeypatch.setattr(
        ssh_module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=returncode)
    )

    assert ssh_module.ssh_session_connected("ssh root@x") is connected


def test_rm_all_treats_a_lost_terminal_as_no(monkeypatch):
    """No answer is not a yes — an EOF at the prompt must not wipe the account."""
    monkeypatch.setattr(rm_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(rm_module.ui, "confirm", lambda message: (_ for _ in ()).throw(EOFError()))

    assert rm_module.human_approved_removing_every_pod([_pod()]) is False
