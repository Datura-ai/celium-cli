"""DAH-2553: renting a GPU must not require the miner/validator stack.

The published path for an agent is: read llms.txt, install the CLI, sign up,
rent. Anything that makes that install heavier or more fragile than it has to be
breaks the chain at step two, so the renter surface must import cleanly with the
optional chain dependencies absent.
"""

import inspect
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from lium.cli.utils import EXIT_CONFIGURATION_ERROR

PYPROJECT = tomllib.loads(Path("pyproject.toml").read_text())
REQUIRED = PYPROJECT["project"]["dependencies"]
OPTIONAL = PYPROJECT["project"].get("optional-dependencies", {})

# Imported by nothing in the package; measured with a grep over lium/ on 2026-08-05.
NEVER_IMPORTED = [
    "bittensor-cli",
    "plotly",
    "plotille",
    "fuzzywuzzy",
    "python-Levenshtein",
    "netaddr",
    "async-substrate-interface",
    "GitPython",
    "Jinja2",
]

# Imported lazily inside functions, and only by provider commands and `lium fund`.
PROVIDER_ONLY = ["bittensor"]


def _names(requirements: list[str]) -> set[str]:
    seps = "><=~!;[ "
    out = set()
    for requirement in requirements:
        name = requirement
        for sep in seps:
            name = name.split(sep)[0]
        out.add(name.lower())
    return out


def test_unused_dependencies_are_not_required():
    required = _names(REQUIRED)
    still_there = sorted(n for n in NEVER_IMPORTED if n.lower() in required)

    assert still_there == [], f"nothing imports these, drop them: {still_there}"


def test_chain_stack_is_optional():
    required = _names(REQUIRED)
    assert not (_names(PROVIDER_ONLY) & required), "bittensor must be an extra, not a base dependency"
    assert "provider" in OPTIONAL, "provider extra must exist for the chain-facing commands"
    assert _names(PROVIDER_ONLY) <= _names(OPTIONAL["provider"])


def test_renting_a_pod_does_not_import_bittensor():
    """The renter path must not drag the chain stack in at import time."""
    probe = (
        "import sys;"
        "import lium.sdk;"
        "from lium.cli.cli import cli;"
        "print('bittensor' in sys.modules)"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False", "importing the CLI/SDK pulled in bittensor"


def test_missing_chain_stack_is_named_not_swallowed(monkeypatch):
    """`provider status` must say why subnet registration reads as unknown.

    The branch is only reachable now that bittensor is an extra: before, the
    import always succeeded, so the ImportError path was dead code that returned
    (None, []) and the caller rendered "unknown" with nothing explaining it.
    """
    import builtins

    from lium.provider.client import _read_metagraph
    from lium.provider.errors import ProviderConfigError

    real_import = builtins.__import__

    def _no_bittensor(name, *args, **kwargs):
        if name == "bittensor":
            raise ImportError("No module named 'bittensor'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_bittensor)

    with pytest.raises(ProviderConfigError) as raised:
        _read_metagraph(hotkey_ss58="5FakeHotkey", netuid=51, factory=None)

    assert "lium.io[provider]" in str(raised.value)


def test_provider_status_surfaces_the_missing_stack_as_a_warning():
    """The raised error must reach the user, not vanish into a bare status line."""
    from lium.provider.client import ProviderClient

    source = inspect.getsource(ProviderClient.status)

    assert "except ProviderError as e:" in source
    assert 'warnings.append(f"metagraph: {e}")' in source


def test_the_missing_stack_message_names_the_right_fix(monkeypatch):
    """Two different situations, two different fixes.

    Absent because it was never installed: add the extra. Absent because it
    cannot build on this interpreter: adding the extra will not help, so the
    message has to say to change Python or take the binary.
    """
    from lium.provider import chain_stack

    monkeypatch.setattr(chain_stack.sys, "version_info", (3, 12, 0, "final", 0))
    assert 'pip install "lium.io[provider]"' in chain_stack.missing_chain_stack_message()

    monkeypatch.setattr(chain_stack.sys, "version_info", (3, 14, 0, "final", 0))
    on_new_python = chain_stack.missing_chain_stack_message()
    assert "3.14" in on_new_python
    assert "lium.io/install.sh" in on_new_python
    assert "pip install" not in on_new_python


def test_the_extra_is_gated_on_python_version():
    """The 3.14 install must not die inside a Rust build with no mention of lium."""
    provider_requirements = OPTIONAL["provider"]

    assert all("python_version < '3.14'" in r for r in provider_requirements), provider_requirements


@pytest.mark.parametrize(
    "command",
    [
        ["fund", "-w", "default", "-a", "1", "-y"],
        ["fund", "--alpha", "-k", "test", "-w", "default", "-a", "1", "-y"],
    ],
)
def test_every_error_path_keeps_the_extra_name_intact(monkeypatch, command):
    """Rich reads square brackets as style tags, so `lium.io[provider]` is fragile.

    The alpha path raises LiumError rather than CliFailure, and only escaping the
    CliFailure branch left it advising `pip install "lium.io"` — the package the
    caller already has.
    """
    import builtins

    from click.testing import CliRunner

    from lium.cli.cli import cli

    real_import = builtins.__import__

    def _no_bittensor(name, *args, **kwargs):
        if name == "bittensor":
            raise ImportError("No module named 'bittensor'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_bittensor)

    result = CliRunner().invoke(cli, command)

    assert "lium.io[provider]" in result.output, result.output
    assert result.exit_code == EXIT_CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "command",
    [
        ["fund", "-w", "default", "-a", "1", "-y", "--json"],
        ["fund", "--alpha", "-k", "test", "-w", "default", "-a", "1", "-y", "--json"],
    ],
)
def test_both_fund_paths_report_the_same_machine_readable_code(monkeypatch, command):
    """A generic error code tells an agent nothing about what to install."""
    import builtins

    from click.testing import CliRunner

    from lium.cli.cli import cli

    real_import = builtins.__import__

    def _no_bittensor(name, *args, **kwargs):
        if name == "bittensor":
            raise ImportError("No module named 'bittensor'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_bittensor)

    result = CliRunner().invoke(cli, command)

    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "provider_extra_missing"
    assert result.exit_code == EXIT_CONFIGURATION_ERROR
