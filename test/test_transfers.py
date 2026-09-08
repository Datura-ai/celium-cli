"""Transfers: rsync that resumes and can be throttled, and pod-to-pod copies.

Multi-GB pulls through the caller's machine are slow and fragile; the pods
themselves are close to each other. `Lium.rsync` gains the rsync options a
long transfer needs, and `Lium.cp` / `lium cp` move data straight from one pod
to another, granting and revoking a one-off key around the copy.
"""

import re
import subprocess
import warnings
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.cp import parsing as cp_parsing
from lium.cli.cp import command as cp_module
from lium.cli.rsync import command as rsync_module
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND
from lium.sdk import Config, Lium, LiumError, PodInfo


def _pod(pod_id: str = "pod-1", name: str = "dev", huid: str = "swift-fox-c8", host: str = "1.2.3.4", port: int = 20299) -> PodInfo:
    return PodInfo(
        id=pod_id, name=name, huid=huid, status="RUNNING", ssh_cmd=f"ssh root@{host} -p {port}",
        ports={}, created_at="", updated_at="", executor=None, template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


SRC = _pod()
DST = _pod("pod-2", "train", "brave-lion-11", "5.6.7.8", 31000)


class _RecordingLium(Lium):
    """Every exec is recorded and answered from a script keyed on a substring."""

    def __init__(self, answers=None):
        super().__init__(Config(api_key="test", ssh_key_path="/home/u/.ssh/id_ed25519"))
        self.sent: list[tuple[str, str]] = []
        self.answers = answers or {}

    def exec(self, pod, *, command, env=None):
        self.sent.append((pod.huid, command))
        for needle, answer in self.answers.items():
            if needle in command:
                return dict(answer)
        return {"success": True, "exit_code": 0, "stdout": "", "stderr": ""}


# --- rsync options ------------------------------------------------------------------------

def test_rsync_options_resume_by_default_and_render_every_flag():
    assert Lium.rsync_options() == ["-az", "--partial"]
    assert Lium.rsync_options(bwlimit=5000, exclude=[".git", "*.pt"], delete=True, progress=True) == [
        "-az", "--partial", "--progress", "--bwlimit=5000",
        "--exclude=.git", "--exclude=*.pt", "--delete",
    ]


def test_rsync_options_never_write_in_place():
    """--inplace drops write-then-rename: a job on the pod could read half a checkpoint."""
    assert "--inplace" not in Lium.rsync_options(partial=True, progress=True, delete=True)


def test_rsync_progress_is_the_flag_openrsync_accepts_too():
    """stock macOS rsync rejects --info=progress2 and the transfer would die before any byte moved."""
    assert "--progress" in Lium.rsync_options(progress=True)
    assert not any(opt.startswith("--info") for opt in Lium.rsync_options(progress=True))


def test_rsync_options_reject_a_non_positive_bwlimit():
    with pytest.raises(ValueError, match="bwlimit"):
        Lium.rsync_options(bwlimit=0)


def _capture_rsync(monkeypatch, returncode: int = 0):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=returncode, stderr="rsync: connection unexpectedly closed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_rsync_pushes_with_the_options_and_captures_output(monkeypatch):
    calls = _capture_rsync(monkeypatch)
    client = _RecordingLium()

    client.rsync(SRC, local="./data/", remote="/workspace/data", bwlimit=1000, exclude=[".git"], delete=True)

    cmd, kwargs = calls[0]
    assert cmd[:6] == ["rsync", "-az", "--partial", "--bwlimit=1000", "--exclude=.git", "--delete"]
    assert cmd[-2:] == ["./data/", "root@1.2.3.4:/workspace/data"]
    assert "-p 20299" in cmd[cmd.index("-e") + 1]
    assert kwargs["capture_output"] is True


def test_rsync_download_reverses_the_endpoints(monkeypatch):
    calls = _capture_rsync(monkeypatch)

    _RecordingLium().rsync(SRC, local="./out", remote="/workspace/out/", download=True)

    assert calls[0][0][-2:] == ["root@1.2.3.4:/workspace/out/", "./out"]


def test_rsync_progress_streams_to_the_terminal(monkeypatch):
    calls = _capture_rsync(monkeypatch)

    _RecordingLium().rsync(SRC, local="./a", remote="/b", progress=True)

    cmd, kwargs = calls[0]
    assert "--progress" in cmd and kwargs["capture_output"] is False


def test_rsync_failure_carries_stderr(monkeypatch):
    _capture_rsync(monkeypatch, returncode=12)

    with pytest.raises(RuntimeError, match="connection unexpectedly closed"):
        _RecordingLium().rsync(SRC, local="./a", remote="/b")


def test_rsync_cli_passes_the_new_flags_through(monkeypatch, tmp_path):
    seen = {}

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [SRC]

        def exec(self, pod, command=None, env=None):
            return {"success": True}

        def rsync(self, pod, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(rsync_module, "Lium", _Lium)
    local = tmp_path / "data"
    local.mkdir()

    result = CliRunner().invoke(cli, [
        "rsync", "dev", str(local), "/workspace/data",
        "--bwlimit", "2000", "--exclude", ".git", "--exclude", "*.pt", "--delete", "--progress",
    ])

    assert result.exit_code == 0, result.output
    assert seen["bwlimit"] == 2000 and seen["exclude"] == [".git", "*.pt"]
    assert seen["delete"] is True and seen["progress"] is True
    assert seen["remote"] == "/workspace/data"


# --- Lium.cp -------------------------------------------------------------------------------

KEYGEN = "ssh-keygen -q -t ed25519"
PUBKEY = "ssh-ed25519 AAAAC3Nza... root@dev"


def _cp_client(rsync_answer=None, **extra):
    answers = {
        KEYGEN: {"success": True, "exit_code": 0, "stdout": PUBKEY + "\n", "stderr": ""},
        "rsync -az": rsync_answer or {"success": True, "exit_code": 0, "stdout": "", "stderr": ""},
    }
    answers.update(extra)
    return _RecordingLium(answers)


def test_cp_grants_a_one_off_key_copies_and_revokes_it():
    client = _cp_client()

    client.cp(SRC, "/workspace/ckpt/", DST, "/workspace/ckpt/", bwlimit=3000, exclude=["*.tmp"])

    pods, commands = zip(*client.sent)
    assert pods == ("swift-fox-c8", "brave-lion-11", "swift-fox-c8", "brave-lion-11", "swift-fox-c8")
    keygen, grant, copy, revoke, remove_key = commands
    assert keygen.startswith(KEYGEN) and "/tmp/lium-cp-" in keygen
    assert PUBKEY in grant and ">> ~/.ssh/authorized_keys" in grant
    assert "rsync -az --partial --bwlimit=3000 '--exclude=*.tmp'" in copy
    assert "-p 31000" in copy and "root@5.6.7.8:/workspace/ckpt/" in copy and "/workspace/ckpt/ " in copy
    assert "StrictHostKeyChecking=no" in copy
    assert "grep -vF" in revoke and "authorized_keys" in revoke
    marker = re.search(r"lium-cp-[0-9a-f]{12}", grant).group(0)
    assert f"authorized_keys.{marker}" in revoke, "the scratch file carries this run's marker"
    assert f"cat ~/.ssh/authorized_keys.{marker} > ~/.ssh/authorized_keys" in revoke
    assert "mv " not in revoke
    assert remove_key.startswith("rm -f /tmp/lium-cp-")


def test_cp_revokes_the_key_even_when_the_copy_fails():
    client = _cp_client(rsync_answer={
        "success": False, "exit_code": 127, "stdout": "", "stderr": "bash: rsync: command not found",
    })

    with pytest.raises(LiumError, match="rsync: command not found"):
        client.cp(SRC, "/a", DST, "/b")

    commands = [c for _, c in client.sent]
    assert any("grep -vF" in c for c in commands)
    assert any(c.startswith("rm -f /tmp/lium-cp-") for c in commands)


def test_cp_stops_before_touching_the_destination_when_keygen_fails():
    client = _RecordingLium({KEYGEN: {"success": False, "exit_code": 1, "stdout": "", "stderr": "no ssh-keygen"}})

    with pytest.raises(LiumError, match="transfer key"):
        client.cp(SRC, "/a", DST, "/b")

    assert all(pod == "swift-fox-c8" for pod, _ in client.sent)


def test_cp_does_not_revoke_what_it_never_granted():
    client = _cp_client(**{">> ~/.ssh/authorized_keys": {
        "success": False, "exit_code": 1, "stdout": "", "stderr": "read-only file system",
    }})

    with pytest.raises(LiumError, match="authorise"):
        client.cp(SRC, "/a", DST, "/b")

    commands = [c for _, c in client.sent]
    assert not any("grep -vF" in c for c in commands)
    assert any(c.startswith("rm -f /tmp/lium-cp-") for c in commands)


def test_cp_within_one_pod_is_a_local_rsync():
    client = _RecordingLium()

    client.cp(SRC, "/workspace/a/", SRC, "/workspace/b/")

    assert client.sent == [("swift-fox-c8", "rsync -az --partial /workspace/a/ /workspace/b/")]


def test_cp_cleanup_failure_is_a_warning_not_the_error():
    client = _cp_client(**{"grep -vF": {"success": False, "exit_code": 1, "stdout": "", "stderr": "denied"}})

    with pytest.warns(UserWarning, match="cleanup") as caught:
        client.cp(SRC, "/a", DST, "/b")

    message = str(caught[0].message)
    assert "still authorised on pod train" in message
    assert "revoke it with: lium exec brave-lion-11 " in message and "grep -vF" in message


def test_revoke_command_keeps_the_other_keys_and_the_mode(tmp_path):
    """Run the revoke line for real against a scratch home."""
    import os
    import stat
    import subprocess as sp

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    keys = ssh_dir / "authorized_keys"
    keys.write_text("ssh-ed25519 AAA renter@laptop\nssh-ed25519 BBB lium-cp-abc123\n")
    keys.chmod(0o600)

    done = sp.run(["/bin/sh", "-c", Lium.revoke_transfer_key_command("lium-cp-abc123")],
                  env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert keys.read_text() == "ssh-ed25519 AAA renter@laptop\n"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert list(ssh_dir.iterdir()) == [keys]


def test_revoke_command_fails_loudly_when_it_cannot_read_the_file(tmp_path):
    import os
    import subprocess as sp

    (tmp_path / ".ssh").mkdir()
    done = sp.run(["/bin/sh", "-c", Lium.revoke_transfer_key_command("lium-cp-abc123")],
                  env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, capture_output=True, text=True)

    assert done.returncode != 0


# --- lium cp -------------------------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("dev:/workspace/x", ("dev", "/workspace/x")),
    ("1:/a", ("1", "/a")),
    ("dev:", ("dev", ".")),
    ("/local/path", (None, "/local/path")),
    (":/x", (None, ":/x")),
])
def test_split_spec(spec, expected):
    assert cp_parsing.split_spec(spec) == expected


def _run_cp(monkeypatch, args, pods=(SRC, DST), cp_result=None):
    calls = []

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return list(pods)

        def cp(self, src_pod, src_path, dst_pod, dst_path, **kwargs):
            calls.append((src_pod.huid, src_path, dst_pod.huid, dst_path, kwargs))
            if isinstance(cp_result, Exception):
                raise cp_result
            return cp_result or {"success": True, "exit_code": 0}

    monkeypatch.setattr(cp_module, "Lium", _Lium)
    return CliRunner().invoke(cli, ["cp", *args]), calls


def test_cp_command_shows_a_failed_revoke_as_a_warning_with_the_command(monkeypatch):
    """A key left on the destination must be visible, with the line that removes it."""
    pods = [SRC, DST]

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return list(pods)

        def cp(self, *a, **k):
            warnings.warn("lium: cleanup on pod train failed (denied): the transfer key 'lium-cp-abc' is still "
                          "authorised on pod train; revoke it with: lium exec brave-lion-11 'grep -vF ...'")
            return {"success": True, "exit_code": 0}

    monkeypatch.setattr(cp_module, "Lium", _Lium)

    result = CliRunner().invoke(cli, ["cp", "dev:/a", "brave-lion-11:/b"])

    assert result.exit_code == 0, result.output
    assert "revoke it with: lium exec brave-lion-11" in result.output
    assert "UserWarning" not in result.output


def test_cp_command_resolves_both_pods_and_passes_options(monkeypatch):
    result, calls = _run_cp(monkeypatch, [
        "dev:/workspace/src", "brave-lion-11:/workspace/", "--exclude", ".git", "--bwlimit", "10", "--delete",
    ])

    assert result.exit_code == 0, result.output
    assert calls == [("swift-fox-c8", "/workspace/src", "brave-lion-11", "/workspace/",
                      {"bwlimit": 10, "exclude": [".git"], "delete": True})]
    assert "swift-fox-c8:/workspace/src" in result.output


def test_cp_command_json_reports_both_endpoints(monkeypatch, tmp_path):
    import json
    from lium.cli import utils
    from lium.cli.utils import store_pod_selection

    # a row number stands for the last `lium ps` in this shell (DAH-2559): record that listing first
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    store_pod_selection([SRC, DST])
    result, _ = _run_cp(monkeypatch, ["1:/a", "2:/b", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["source"] == {"pod": "swift-fox-c8", "path": "/a"}
    assert payload["destination"] == {"pod": "brave-lion-11", "path": "/b"}


def test_cp_command_refuses_a_local_path(monkeypatch):
    result, calls = _run_cp(monkeypatch, ["./local", "dev:/x"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "lium scp" in result.output and calls == []


def test_cp_command_fails_on_an_unknown_pod(monkeypatch):
    result, calls = _run_cp(monkeypatch, ["dev:/x", "nope:/y"])

    assert result.exit_code == EXIT_POD_NOT_FOUND and calls == []


def test_cp_command_surfaces_a_failed_copy(monkeypatch):
    result, _ = _run_cp(monkeypatch, ["dev:/x", "train:/y"], cp_result=LiumError("Copy failed: rsync: command not found"))

    assert result.exit_code != 0
    assert "command not found" in result.output
