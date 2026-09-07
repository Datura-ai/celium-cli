"""Environment values reach the pod verbatim.

``exec(env={...})`` used to build ``export K="v"``: a token with ``$``, a
password with a backtick or a JSON blob with ``"`` was interpreted by the
remote shell before the command ran. Values are now shell-quoted, names are
validated up front, and ``exec_all`` reports failures in the same shape as
successes.
"""

import shlex
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import exec as exec_module
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.sdk import Lium


def _client() -> Lium:
    return Lium.__new__(Lium)  # no config, no network: _prep_command is pure


# --- _prep_command -------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "plain",
    "has spaces",
    'say "hi"',
    "it's",
    "$HOME and ${USER}",
    "`whoami`",
    "$(id)",
    "a;b && c | d",
    '{"key": "value", "n": 1}',
    "",
])
def test_values_survive_the_shell_unchanged(value):
    prepared = _client()._prep_command("printenv V", env={"V": value})

    # Run the exported prefix through a real shell and read the variable back.
    out = subprocess.run(["sh", "-c", prepared], capture_output=True, text=True, check=True)
    assert out.stdout == value + "\n"


def test_prefix_is_exports_joined_before_the_command():
    prepared = _client()._prep_command("python train.py", env={"A": "1", "B": "two words"})

    assert prepared == f"export A=1 && export B={shlex.quote('two words')} && python train.py"


def test_no_env_leaves_the_command_alone():
    assert _client()._prep_command("ls", env=None) == "ls"
    assert _client()._prep_command("ls", env={}) == "ls"


@pytest.mark.parametrize("name", ["1ABC", "A-B", "A B", "A=B", "", "$X"])
def test_invalid_names_are_rejected_before_anything_is_sent(name):
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        _client()._prep_command("ls", env={name: "x"})


def test_non_string_values_are_stringified():
    assert _client()._prep_command("ls", env={"N": 3}) == "export N=3 && ls"


# --- exec_all ------------------------------------------------------------------------------

def test_exec_all_failure_entries_carry_the_pod_id_like_successes(monkeypatch):
    client = _client()
    good = SimpleNamespace(id="pod-good")
    bad = SimpleNamespace(id="pod-bad")

    def fake_exec(pod, *, command, env=None):
        if pod is bad:
            raise OSError("ssh: connect to host: Connection refused")
        return {"stdout": "ok\n", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(client, "exec", fake_exec)

    results = client.exec_all([good, bad], command="true")

    by_pod = {r["pod"]: r for r in results}
    assert set(by_pod) == {"pod-good", "pod-bad"}
    assert by_pod["pod-good"]["success"] is True
    assert by_pod["pod-bad"] == {
        "pod": "pod-bad",
        "error": "ssh: connect to host: Connection refused",
        "success": False,
    }


# --- lium exec -e ----------------------------------------------------------------------------

def test_cli_rejects_an_invalid_env_name_with_a_configuration_error(monkeypatch):
    class _Lium:
        def __init__(self, *a, **k):
            raise AssertionError("must fail before touching the SDK")

    monkeypatch.setattr(exec_module, "Lium", _Lium)

    result = CliRunner().invoke(cli, ["exec", "my-pod", "-e", "1BAD=x", "echo hi"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "Invalid env name '1BAD'" in result.output


def test_cli_passes_values_with_shell_characters_through_untouched(monkeypatch):
    seen = {}

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name="my-pod")]

        def exec(self, pod, command=None, env=None):
            seen.update(env)
            return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(exec_module, "Lium", _Lium)

    result = CliRunner().invoke(
        cli, ["exec", "my-pod", "-e", "TOKEN=abc$def`x`", "-e", "JSON={\"a\": 1}", "echo hi"]
    )

    assert result.exit_code == 0, result.output
    assert seen == {"TOKEN": "abc$def`x`", "JSON": '{"a": 1}'}
