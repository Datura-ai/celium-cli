"""DAH-2902: the docs and README write ``--json``/``--yes`` after the subcommand
(``lium provider status --json``, ``lium provider config opt-in --yes``); the CLI only
accepted them before it and answered ``No such option``. Both positions must work."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.cli.volumes.new.command import volumes_new_command
from lium.provider.client import ProviderClient


class _Portal:
    def __init__(self, *, get_body=None, post_body=None):
        self._get_body = get_body
        self._post_body = post_body
        self.posts: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        return self._get_body or {}

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        return self._post_body or {}


@pytest.fixture
def patched_config_client(monkeypatch, fake_signer, tmp_token_store):
    def _factory(portal: _Portal):
        def _builder(ctx):
            return ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal)  # type: ignore[arg-type]

        monkeypatch.setattr("lium.cli.provider.config.build_client", _builder)
        return portal

    return _factory


def test_json_after_the_subcommand(patched_config_client) -> None:
    patched_config_client(_Portal(get_body={"miner_hotkey": "5Foo", "miner_uid": 7, "email": "a@b.co"}))
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "config", "show", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["miner_hotkey"] == "5Foo"


def test_yes_after_the_subcommand_satisfies_the_persona_gate(patched_config_client) -> None:
    portal = patched_config_client(
        _Portal(post_body={"miner_hotkey": "5Foo", "miner_coldkey": "5Bar", "central_miner_ip": "1.2.3.4", "central_miner_port": 9090})
    )
    # the documented line, verbatim (docs/developers/cli/reference/provider.md, README.md)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "config", "opt-in", "--yes"])
    assert result.exit_code == 0, result.output
    assert portal.posts == [("/miners/opt-in", {"opt_in_status": True}, True)]


def test_short_yes_and_json_after_a_nested_subcommand(patched_config_client) -> None:
    portal = patched_config_client(_Portal(post_body={"miner_hotkey": "5Foo", "miner_coldkey": "5Bar", "central_miner_ip": "1.2.3.4", "central_miner_port": 9090}))
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "config", "opt-out", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip())["ok"] is True
    assert portal.posts == [("/miners/opt-in", {"opt_in_status": False}, True)]


def test_flags_before_the_subcommand_still_work(patched_config_client) -> None:
    portal = patched_config_client(_Portal(post_body={"miner_hotkey": "5Foo", "miner_coldkey": "5Bar", "central_miner_ip": "1.2.3.4", "central_miner_port": 9090}))
    result = CliRunner().invoke(provider_command, ["-y", "--json", "--hotkey", "hk1", "config", "opt-in"])
    assert result.exit_code == 0, result.output
    assert portal.posts[0][1] == {"opt_in_status": True}


def test_volumes_new_accepts_description(monkeypatch) -> None:
    """README: ``lium volumes new mydata --description "My dataset"`` (the flag was ``--desc`` only)."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr("lium.cli.volumes.new.command.ensure_config", lambda: None)
    monkeypatch.setattr("lium.cli.volumes.new.command.Lium", lambda: object())
    monkeypatch.setattr("lium.cli.volumes.new.command.ui.load", lambda _msg, fn: fn())

    class _Action:
        def execute(self, ctx):
            seen.update(ctx)

    monkeypatch.setattr("lium.cli.volumes.new.command.CreateVolumeAction", _Action)
    result = CliRunner().invoke(volumes_new_command, ["mydata", "--description", "My dataset"])
    assert result.exit_code == 0, result.output
    assert seen["name"] == "mydata"
    assert seen["description"] == "My dataset"
