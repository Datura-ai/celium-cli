"""`lium init --api-key` — headless auth for an agent that already holds a key."""

import json
import stat

import pytest
import requests
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init import actions as init_actions
from lium.cli.init import command as init_command
from lium.cli.settings import ConfigManager
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import LiumAuthError, LiumServerError


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A fresh HOME with no ~/.lium and no key in the environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)
    fresh = ConfigManager()
    monkeypatch.setattr(init_actions, "config", fresh)
    monkeypatch.setattr(init_command, "config", fresh)
    return fresh


@pytest.fixture
def ssh_setup_ok(monkeypatch, home):
    def _execute(self, ctx):
        home.set("ssh.key_path", str(home.config_dir.parent / ".ssh" / "id_ed25519"))
        return ActionResult(ok=True, data={"already_configured": False})

    monkeypatch.setattr(init_actions.SetupSshKeyAction, "execute", _execute)


class _Client:
    """Stands in for lium.sdk.Lium: records the key it was built with, answers /users/me as told."""

    seen: list = []
    sources: list = []
    outcome: object = 12.5

    def __init__(self, config, source="sdk"):
        self.config = config
        _Client.seen.append(config.api_key)
        _Client.sources.append(source)

    def balance(self):
        if isinstance(_Client.outcome, Exception):
            raise _Client.outcome
        return _Client.outcome


@pytest.fixture
def api(monkeypatch):
    _Client.seen = []
    _Client.sources = []
    _Client.outcome = 12.5
    monkeypatch.setattr("lium.sdk.Lium", _Client)
    return _Client


def test_api_key_is_checked_then_saved_and_ssh_is_set_up(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_good"]                      # checked with the key that was passed, not the env
    assert home.get("api.api_key") == "sk_good"
    assert stat.S_IMODE(home.config_file.stat().st_mode) == 0o600
    assert "saved to" in result.output and "SSH key:" in result.output


def test_api_key_json_names_source_config_and_ssh_key(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["api_key_source"] == "flag"
    assert payload["env_key"] is None
    assert payload["config_path"] == str(home.config_file)
    assert payload["ssh_key_path"].endswith("id_ed25519")


def test_a_refused_key_is_not_saved_and_exits_3(home, ssh_setup_ok, api):
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_bad"])

    assert result.exit_code == EXIT_API_ERROR
    assert home.get("api.api_key") is None
    assert not home.config_file.exists()
    assert "refused" in result.output


def test_a_refused_key_under_json_is_an_envelope_on_stderr(home, ssh_setup_ok, api):
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_bad", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_api_key"


def test_the_check_uses_the_flag_key_not_the_environment_or_the_file(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")
    home.set("api.api_key", "sk_file")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_flag"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_flag"]
    assert api.sources == ["cli"]
    assert home.get_all()["api"]["api_key"] == "sk_flag"
    assert "LIUM_API_KEY is set and wins" in result.output

    payload = json.loads(CliRunner().invoke(cli, ["init", "--api-key", "sk_flag2", "--json"]).output)
    assert payload["env_key"] == "LIUM_API_KEY"      # the JSON caller sees the same warning as a field


@pytest.mark.parametrize("outcome", [
    requests.ConnectionError("Connection refused"),      # the SDK lets transport errors through raw
    LiumServerError("Server error: 502"),                # a non-auth answer says nothing about the key
])
def test_an_unreachable_api_does_not_save_the_key(home, ssh_setup_ok, api, outcome):
    api.outcome = outcome

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_unknown", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert home.get("api.api_key") is None
    assert json.loads(result.output)["error"]["code"] == "api_unreachable"


def test_an_empty_key_is_refused_without_a_request(monkeypatch, home, ssh_setup_ok, api):
    """`--api-key "$KEY"` with KEY unset must not fall through to the browser flow."""
    def _no_browser(self, ctx):
        raise AssertionError("browser flow must not run for --api-key ''")

    monkeypatch.setattr(init_actions.SetupApiKeyAction, "execute", _no_browser)

    result = CliRunner().invoke(cli, ["init", "--api-key", "", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.output)["error"]["code"] == "empty_api_key"
    assert api.seen == []
    assert home.get("api.api_key") is None


def test_a_key_with_a_newline_inside_is_refused_without_a_request_and_not_echoed(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_ab\rcd", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.output)["error"]["code"] == "invalid_api_key"
    assert "sk_ab" not in result.output
    assert api.seen == []
    assert home.get("api.api_key") is None


def test_api_key_replaces_a_previously_saved_key(home, ssh_setup_ok, api):
    home.set("api.api_key", "sk_old")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_new"])

    assert result.exit_code == 0, result.output
    assert home.get("api.api_key") == "sk_new"


def test_api_key_with_a_browser_option_is_refused(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good", "--no-browser"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert api.seen == []


def test_env_key_skips_the_browser_and_saves_nothing(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")

    def _no_browser(self, ctx):
        raise AssertionError("browser flow must not run when LIUM_API_KEY is set")

    monkeypatch.setattr(init_actions.SetupApiKeyAction, "execute", _no_browser)

    result = CliRunner().invoke(cli, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["api_key_source"] == "env"
    assert payload["env_key"] == "LIUM_API_KEY"
    assert home.get_all().get("api", {}).get("api_key") is None   # the key is not written
    assert home.get("ssh.key_path")                              # the SSH half still happens
    assert api.seen == []


def test_env_key_text_names_the_variable_and_the_file(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "LIUM_API_KEY" in result.output and "not written to" in result.output


def test_the_cli_only_alias_is_not_a_key_source_for_init(monkeypatch, home, ssh_setup_ok, api):
    """The SDK reads LIUM_API_KEY only; an init that trusted LIUM_API_API_KEY would exit 0 and leave
    every following command with no key. The real actions run here — nothing is stubbed but the
    browser, which must not open."""
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_alias")
    monkeypatch.setattr(init_actions, "browser_auth", lambda: pytest.fail("browser must not open"))

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "unsupported" in result.output.lower() or "LIUM_API_KEY" in result.output
    assert home.get_all().get("api", {}).get("api_key") is None
    assert api.seen == []


def test_the_alias_next_to_a_saved_key_is_not_an_error(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_alias")
    home.set("api.api_key", "sk_file")

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "already saved in" in result.output


def test_env_key_wins_over_session_and_no_browser(monkeypatch, home, ssh_setup_ok, api):
    """With LIUM_API_KEY exported the session/URL actions did nothing and said nothing (already
    configured); now every init flow reports the environment as the source."""
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")
    monkeypatch.setattr(init_actions, "init_auth", lambda: pytest.fail("no auth session must be requested"))
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: pytest.fail("no session must be polled"))

    for args in (["init", "--session", "abc", "--json"], ["init", "--no-browser", "--json"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, (args, result.output)
        assert json.loads(result.output)["api_key_source"] == "env", args


def test_json_needs_a_key_source(home, ssh_setup_ok, api):
    """The browser flows print for a person; --json with them would mix text and JSON on stdout."""
    for args in (["init", "--json"], ["init", "--no-browser", "--json"], ["init", "--session", "abc", "--json"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == EXIT_CONFIGURATION_ERROR, args
        assert json.loads(result.output)["error"]["code"] == "conflicting_options", args
    assert api.seen == []


def test_browser_failure_names_the_headless_way(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setattr(
        init_actions.SetupApiKeyAction, "execute",
        lambda self, ctx: ActionResult(ok=False, data={}, error="Authentication failed"),
    )

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 1
    assert "--api-key" in result.output and "LIUM_API_KEY" in result.output


def test_config_dir_is_created_with_its_parents(monkeypatch, tmp_path):
    """A fresh container's HOME may not exist yet; every command imports the config at start."""
    monkeypatch.setenv("HOME", str(tmp_path / "not-yet"))

    manager = ConfigManager()

    assert manager.config_dir == tmp_path / "not-yet" / ".lium"
    assert manager.config_dir.is_dir()
