"""DAH-2553: renting a GPU must not require the miner/validator stack.

The published path for an agent is: read llms.txt, install the CLI, sign up,
rent. Anything that makes that install heavier or more fragile than it has to be
breaks the chain at step two, so the renter surface must import cleanly with the
optional chain dependencies absent.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

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
