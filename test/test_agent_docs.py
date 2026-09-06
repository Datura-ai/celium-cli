"""docs/agents.md must describe flags that exist.

The agent guide says which flags are on main and which are on a pending
branch. The "main" claims are checked against the click command tree so the
page cannot drift from the CLI it documents.
"""

from pathlib import Path

import pytest

from lium.cli.cli import cli

ROOT = Path(__file__).resolve().parent.parent
AGENTS_DOC = ROOT / "docs" / "agents.md"


def _params(command_path: str) -> set[str]:
    command = cli
    for name in command_path.split():
        command = command.commands[name]
    return {opt for param in command.params for opt in param.opts}


@pytest.mark.parametrize("command, flag", [
    ("ps", "--format"),
    ("ls", "--format"),
    ("ls", "--gpu"),
    ("ls", "--count"),
    ("exec", "--json"),
    ("describe", "--json"),
    ("balance", "--json"),
    ("up", "--yes"),
    ("up", "--no-ssh"),
    ("up", "--ttl"),
    ("up", "--until"),
    ("up", "--name"),
    ("rm", "--yes"),
    ("rm", "--all"),
    ("scp", "--download"),
])
def test_flags_the_guide_attributes_to_main_exist(command, flag):
    assert flag in _params(command), f"docs/agents.md relies on 'lium {command} {flag}'"


def test_guide_exists_and_readme_links_it():
    readme = (ROOT / "README.md").read_text()

    assert AGENTS_DOC.exists()
    assert "docs/agents.md" in readme


def test_guide_covers_the_lifecycle_and_the_gotchas():
    text = AGENTS_DOC.read_text()

    for needle in (
        "LIUM_API_KEY", "--format json", "--ttl", "--yes", "--no-ssh", "lium rm",
        "exit_code", '"ok": false', "/workspace", "HF_HOME", "PEP 668", "cu128", "FlashAttention-3",
        "nohup setsid", "< /dev/null",
    ):
        assert needle in text, needle
