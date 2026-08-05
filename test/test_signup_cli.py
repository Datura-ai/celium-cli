"""DAH-2587: `lium signup` creates an account and stores the API key it mints."""

import json
import pathlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init.actions import SetupSshKeyAction
from lium.cli.signup import actions as signup_actions


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def stored_config(monkeypatch):
    """Config double: empty to start, records what signup writes into it."""
    stored = {}

    class FakeConfig:
        def get(self, key, default=None):
            return stored.get(key, default)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(signup_actions, "config", FakeConfig())
    return stored


@pytest.fixture
def ssh_setup_ok(monkeypatch):
    monkeypatch.setattr(
        "lium.cli.init.actions.SetupSshKeyAction.execute",
        lambda self, ctx: ActionResult(ok=True, data={"already_configured": True}),
    )


def test_signup_stores_key_read_back_from_keys_endpoint(monkeypatch, stored_config, ssh_setup_ok):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/users"):
            return FakeResponse(200, {"msg": "success"})
        return FakeResponse(200, {"token": "jwt-token"})

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(200, [{"name": "Default", "key": "sk_minted"}])

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setattr(signup_actions.requests, "get", fake_get)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert stored_config["api.api_key"] == "sk_minted"
    assert [c.rsplit("/api", 1)[-1] for c in calls] == ["/users", "/users/login", "/keys"]


def test_signup_uses_key_returned_by_signup_response(monkeypatch, stored_config, ssh_setup_ok):
    """A backend that returns the key inline makes login + GET /keys unnecessary."""
    posted = []

    def fake_post(url, **kwargs):
        posted.append(url)
        return FakeResponse(200, {"msg": "success", "api_key": "sk_inline"})

    def fail_get(url, **kwargs):
        raise AssertionError("GET /keys must be skipped when signup returns the key")

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setattr(signup_actions.requests, "get", fail_get)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert stored_config["api.api_key"] == "sk_inline"
    assert len(posted) == 1


def test_signup_json_reports_credentials_and_next_steps(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"msg": "success", "api_key": "sk_inline"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["api_key"] == "sk_inline"
    assert payload["email"] == "ada@example.com"
    assert payload["password"]
    assert any("verification link" in step for step in payload["next_steps"])


def test_signup_generates_a_password_when_omitted(monkeypatch, stored_config, ssh_setup_ok):
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return FakeResponse(200, {"api_key": "sk_inline"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    assert len(sent["password"]) == signup_actions.PASSWORD_LENGTH
    assert sent["name"] == "ada"
    assert json.loads(result.output)["password"] == sent["password"]


def test_signup_refuses_when_a_key_is_already_configured(monkeypatch, stored_config, ssh_setup_ok):
    stored_config["api.api_key"] = "sk_existing"

    def fail_post(url, **kwargs):
        raise AssertionError("signup must not call the API when a key is already configured")

    monkeypatch.setattr(signup_actions.requests, "post", fail_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert stored_config["api.api_key"] == "sk_existing"


def test_signup_surfaces_backend_rejection(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(400, {"detail": "User already exists with the same email"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "api.api_key" not in stored_config


def test_signup_surfaces_rate_limit(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(429, {"detail": "Too many requests."}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "Too many signups" in result.output


def test_signup_targets_the_configured_base_url(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setenv("LIUM_BASE_URL", "https://staging.lium.io/api")
    seen = []

    def fake_post(url, **kwargs):
        seen.append(url)
        return FakeResponse(200, {"api_key": "sk_inline"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert seen == ["https://staging.lium.io/api/users"]


def test_completion_notice_goes_to_stderr(tmp_path, monkeypatch, capsys):
    """The first run configures shell completions; that notice must not land in --json stdout."""
    from lium.cli import completion

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")

    completion.ensure_completion()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Shell completions" in captured.err


def test_ssh_setup_creates_the_ssh_directory_when_missing(tmp_path, monkeypatch):
    """A fresh machine has no ~/.ssh; ssh-keygen fails unless it is created first."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    stored = {}
    monkeypatch.setattr(
        "lium.cli.init.actions.config",
        type("C", (), {"get": lambda self, k, d=None: stored.get(k, d),
                       "set": lambda self, k, v: stored.__setitem__(k, v)})(),
    )

    result = SetupSshKeyAction().execute({})

    assert result.ok, result.error
    assert (tmp_path / ".ssh" / "id_ed25519").exists()
    assert stored["ssh.key_path"] == str(tmp_path / ".ssh" / "id_ed25519")
