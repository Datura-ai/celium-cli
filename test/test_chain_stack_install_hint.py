"""DAH-2943: without the chain stack, `lium provider portal login` said
`pip install "lium.io[provider]"` and appended the renter's `Run lium init` hint.
Providers install with mine.sh (`uv tool install lium.io`, a tool venv pip cannot
reach) or the curl binary (which ships the stack); the fix named must fit the install.
"""

from __future__ import annotations

import sys

import pytest

from lium.provider import chain_stack
from lium.provider.errors import ProviderConfigError
from lium.provider.wallet import load_hotkey_keypair


@pytest.fixture(autouse=True)
def _supported_python(monkeypatch):
    # the 3.14 branch has its own message; these tests are about the install method
    monkeypatch.setattr(sys, "version_info", (3, 12, 3, "final", 0))
    monkeypatch.delenv("UV_TOOL_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_uv_tool_install_gets_a_uv_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".local/share/uv/tools/lium-io/bin/python"))
    assert chain_stack.install_command() == 'uv tool install --force "lium.io[provider]"'


def test_relocated_uv_tool_dir_is_recognised(monkeypatch, tmp_path):
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tools/lium-io/bin/python"))
    assert chain_stack.install_command() == 'uv tool install --force "lium.io[provider]"'


def test_pipx_install_gets_a_pipx_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".local/pipx/venvs/lium-io/bin/python"))
    assert chain_stack.install_command() == 'pipx install --force "lium.io[provider]"'


def test_frozen_binary_points_at_the_installer(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "https://lium.io/install.sh" in chain_stack.install_command()
    assert "pip install" not in chain_stack.install_command()


def test_plain_venv_keeps_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv/bin/python"))
    assert chain_stack.install_command() == 'pip install "lium.io[provider]"'


def test_missing_stack_error_carries_the_fix_and_no_renter_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / ".local/share/uv/tools/lium-io/bin/python"))
    monkeypatch.setitem(sys.modules, "bittensor", None)  # `import bittensor` raises ImportError
    with pytest.raises(ProviderConfigError) as excinfo:
        load_hotkey_keypair("default", "default")
    error = excinfo.value
    assert error.code == "CONFIG_MISSING"
    assert 'uv tool install --force "lium.io[provider]"' in error.message
    assert error.hint == ""  # not "Run `lium init` …"
    assert "lium init" not in str(error)
