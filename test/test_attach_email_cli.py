"""DAH-2654: `lium attach-email` adds an email + password login to a key-only account."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.signup import actions as signup_actions


class FakeResponse:
    def __init__(self, status_code: int = 201, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture
def stored_config(monkeypatch):
    """Config double: the stored API key is what names the account to attach to."""
    stored = {"api.api_key": "sk_wallet"}

    class FakeConfig:
        def get(self, key, default=None):
            return stored.get(key, default)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(signup_actions, "config", FakeConfig())
    return stored


def test_attach_email_posts_the_credential_under_the_stored_key(monkeypatch, stored_config):
    sent = {}

    def fake_post(url, **kwargs):
        sent["url"] = url
        sent["json"] = kwargs["json"]
        sent["headers"] = kwargs["headers"]
        return FakeResponse(201, {"id": "user-1", "email": "ada@example.com"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(
        cli, ["attach-email", "--email", "ada@example.com", "--password", "s3cret-pw"]
    )

    assert result.exit_code == 0, result.output
    assert sent["url"].rsplit("/api", 1)[-1] == "/users/me/email-password"
    assert sent["json"] == {"email": "ada@example.com", "password": "s3cret-pw"}
    assert sent["headers"]["X-API-Key"] == "sk_wallet"


def test_attach_email_without_an_api_key_points_at_signup_key(monkeypatch, stored_config):
    stored_config.pop("api.api_key")

    def fail_post(url, **kwargs):
        raise AssertionError("attach-email must not call the API without an API key")

    monkeypatch.setattr(signup_actions.requests, "post", fail_post)

    result = CliRunner().invoke(cli, ["attach-email", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "lium signup-key" in " ".join(result.output.split())


def test_attach_email_surfaces_an_account_that_already_has_an_email(monkeypatch, stored_config):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(409, {"detail": {"email": "Account already has an email address."}}),
    )

    result = CliRunner().invoke(cli, ["attach-email", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "Account already has an email address." in " ".join(result.output.split())


def test_attach_email_surfaces_an_email_taken_by_another_account(monkeypatch, stored_config):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(409, {"detail": {"email": "Email is already in use."}}),
    )

    result = CliRunner().invoke(cli, ["attach-email", "--email", "ada@example.com", "--json"])

    assert result.exit_code != 0
    assert "Email is already in use." in json.loads(result.stderr)["error"]["message"]


def test_attach_email_generates_a_password_when_omitted(monkeypatch, stored_config):
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return FakeResponse(201, {"id": "user-1"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["attach-email", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0, result.output
    assert len(sent["password"]) == signup_actions.PASSWORD_LENGTH
    assert json.loads(result.output)["password"] == sent["password"]


def test_attach_email_reports_the_password_when_the_request_times_out(monkeypatch, stored_config):
    """The backend mails the user inside the request, so a read timeout can still leave the login attached."""
    def timing_out_post(url, **kwargs):
        raise signup_actions.requests.Timeout("read timed out")

    monkeypatch.setattr(signup_actions.requests, "post", timing_out_post)

    result = CliRunner().invoke(
        cli, ["attach-email", "--email", "ada@example.com", "--password", "s3cret-pw"]
    )

    assert result.exit_code != 0
    plain = " ".join(result.output.split())
    assert "s3cret-pw" in plain
    assert "ada@example.com" in plain
