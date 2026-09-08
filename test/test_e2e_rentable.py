"""`e2e/conftest.py`'s `rentable()` — the guard that picks the node the live suite rents (DAH-3151).

The e2e suite only ever calls it on live listings, so the unit suite carries the negative controls: a node in an
excluded country, an excluded executor (by id or by huid), no GPU, over the price cap — and the accept case.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent.parent / "e2e" / "conftest.py"


def _load(monkeypatch, **env):
    """A fresh import of e2e/conftest.py with the given E2E_* variables (it reads them at import)."""
    for name in ("E2E_EXCLUDE_COUNTRIES", "E2E_EXCLUDE_EXECUTORS", "E2E_MAX_PRICE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("e2e_conftest_under_test", CONFTEST)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)   # its dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "gpu_count, price, country, executor_id, huid, expected",
    [
        (1, 0.30, "Ukraine", "abc-123", "eager-comet-56", True),
        (1, 0.30, "Belarus", "abc-123", "eager-comet-56", False),      # the default exclusion
        (2, 0.30, "belarus", "abc-123", "eager-comet-56", False),      # case-insensitive
        (1, 0.30, "Russia", "abc-123", "eager-comet-56", False),
        (1, 0.30, "RU", "abc-123", "eager-comet-56", False),           # the CLI shows the ISO code when the listing has no country name
        (1, 0.30, "BY", "abc-123", "eager-comet-56", False),
        (1, 0.30, "Ukraine", "abc-123", "Brave-Shark-FF", False),      # excluded by huid, case-insensitive
        (1, 0.30, "Ukraine", "DEF-456", "eager-comet-56", False),      # excluded by id
        (0, 0.30, "Ukraine", "abc-123", "eager-comet-56", False),      # no GPU
        (1, 0.51, "Ukraine", "abc-123", "eager-comet-56", False),      # over E2E_MAX_PRICE
        (1, None, "Ukraine", "abc-123", "eager-comet-56", False),      # no price → never the cheapest
    ],
)
def test_rentable_with_the_defaults_and_an_executor_exclusion(monkeypatch, gpu_count, price, country, executor_id, huid, expected):
    conftest = _load(monkeypatch, E2E_EXCLUDE_EXECUTORS="brave-shark-ff,def-456")
    assert conftest.rentable(gpu_count, price, country, executor_id, huid) is expected


def test_the_country_exclusion_is_on_by_default_and_an_explicit_empty_value_lifts_it(monkeypatch):
    assert _load(monkeypatch).EXCLUDE_COUNTRIES == {"russia", "belarus", "ru", "by"}
    conftest = _load(monkeypatch, E2E_EXCLUDE_COUNTRIES="")
    assert conftest.EXCLUDE_COUNTRIES == set()
    assert conftest.rentable(1, 0.30, "Belarus", "abc-123", "eager-comet-56") is True


def test_the_ci_job_states_the_same_exclusion_as_the_default(monkeypatch):
    """ci.yml carries the list so the job says what it does; it must not drift from conftest's default."""
    ci = (CONFTEST.parent.parent / ".github" / "workflows" / "ci.yml").read_text()
    conftest = _load(monkeypatch)
    line = next(line for line in ci.splitlines() if "E2E_EXCLUDE_COUNTRIES:" in line)
    stated = {c.strip().lower() for c in line.split('"')[1].split(",")}
    assert stated == conftest.EXCLUDE_COUNTRIES
