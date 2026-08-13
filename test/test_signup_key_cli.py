"""DAH-2654: `lium signup-key` registers an account owned by an existing Bittensor hotkey."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.signup import actions as signup_actions
from lium.provider.errors import ProviderError

ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeKeypair:
    """Stands in for a bittensor hotkey keypair: records what it was asked to sign."""

    def __init__(self, address: str = ADDRESS):
        self.ss58_address = address
        self.signed: list = []

    def sign(self, message):
        self.signed.append(message)
        return b"\xde\xad\xbe\xef"


@pytest.fixture
def stored_config(monkeypatch):
    """Config double: empty to start, records what the signup writes into it."""
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


@pytest.fixture
def keypair(monkeypatch):
    """Wallet double: no ~/.bittensor keyfile is read in the tests."""
    loaded = FakeKeypair()
    monkeypatch.setattr(signup_actions, "load_hotkey_keypair", lambda coldkey, hotkey: loaded)
    return loaded


def test_signup_key_stores_the_key_returned_by_the_wallet_signup(monkeypatch, stored_config, ssh_setup_ok, keypair):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me", "expires_at": "2026-08-13T10:05:00"})
        return FakeResponse(200, {"success": True, "api_key": "sk_wallet", "is_new_user": True})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code == 0, result.output
    assert stored_config["api.api_key"] == "sk_wallet"
    assert [c.rsplit("/api", 1)[-1] for c in calls] == ["/auth/wallet-challenge", "/auth/wallet-signup"]


def test_signup_key_signs_the_challenge_message_verbatim(monkeypatch, stored_config, ssh_setup_ok, keypair):
    """The backend verifies the signature over the exact message it minted, as bare hex without 0x."""
    message = "lium wallet challenge nonce=abc123 issued=2026-08-13T10:00:00"
    sent = {}

    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            sent["challenge"] = kwargs["json"]
            return FakeResponse(200, {"nonce": "abc123", "message": message, "expires_at": "2026-08-13T10:05:00"})
        sent["signup"] = kwargs["json"]
        return FakeResponse(200, {"api_key": "sk_wallet"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(
        cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent", "--name", "agent-01"]
    )

    assert result.exit_code == 0, result.output
    assert keypair.signed == [message]
    assert sent["challenge"] == {"address": ADDRESS}
    assert sent["signup"] == {
        "address": ADDRESS,
        "nonce": "abc123",
        "signature": "deadbeef",
        "name": "agent-01",
    }


def test_signup_key_refuses_when_a_key_is_already_configured(monkeypatch, stored_config, ssh_setup_ok, keypair):
    stored_config["api.api_key"] = "sk_existing"

    def fail_post(url, **kwargs):
        raise AssertionError("signup-key must not call the API when a key is already configured")

    monkeypatch.setattr(signup_actions.requests, "post", fail_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert stored_config["api.api_key"] == "sk_existing"
    assert "already configured" in " ".join(result.output.split())


def test_signup_key_reports_a_wallet_that_cannot_be_opened(monkeypatch, stored_config, ssh_setup_ok):
    """No keypair, no signature — the backend is never called."""
    def missing_wallet(coldkey, hotkey):
        raise ProviderError(f"could not open wallet name={coldkey!r} hotkey={hotkey!r}")

    monkeypatch.setattr(signup_actions, "load_hotkey_keypair", missing_wallet)
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: (_ for _ in ()).throw(AssertionError("no wallet, no request")),
    )

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "missing", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert "could not open wallet" in " ".join(result.output.split())
    assert "api.api_key" not in stored_config


def test_signup_key_surfaces_an_address_already_registered(monkeypatch, stored_config, ssh_setup_ok, keypair):
    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me"})
        return FakeResponse(400, {"detail": {"address": "Wallet address already registered."}})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert "Wallet address already registered." in " ".join(result.output.split())
    assert "api.api_key" not in stored_config


def test_signup_key_surfaces_an_expired_challenge(monkeypatch, stored_config, ssh_setup_ok, keypair):
    """The challenge is single-use and lives 300 s; a stale one comes back as 401."""
    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me"})
        return FakeResponse(401, {"detail": {"challenge": "Invalid or expired wallet challenge."}})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert "Invalid or expired wallet challenge." in " ".join(result.output.split())


def test_signup_key_surfaces_a_rejected_address(monkeypatch, stored_config, ssh_setup_ok, keypair):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(400, {"detail": {"address": "Invalid SS58 address."}}),
    )

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert "Invalid SS58 address." in " ".join(result.output.split())


def test_signup_key_surfaces_rate_limit(monkeypatch, stored_config, ssh_setup_ok, keypair):
    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me"})
        return FakeResponse(429, {"detail": "Too many requests."})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code != 0
    assert "Too many signups" in result.output


def test_signup_key_announces_the_credit_the_same_way_the_email_signup_does(
    monkeypatch, stored_config, ssh_setup_ok, keypair
):
    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me"})
        return FakeResponse(200, {"api_key": "sk_wallet", "signup_credit_granted": True})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent"])

    assert result.exit_code == 0, result.output
    assert "$5 signup credit was granted" in result.output


def test_signup_key_json_reports_the_address_and_the_key(monkeypatch, stored_config, ssh_setup_ok, keypair):
    def fake_post(url, **kwargs):
        if url.endswith("/auth/wallet-challenge"):
            return FakeResponse(200, {"nonce": "n-1", "message": "sign me"})
        return FakeResponse(200, {"api_key": "sk_wallet", "signup_credit_granted": False, "is_new_user": True})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup-key", "--coldkey", "default", "--hotkey", "agent", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["address"] == ADDRESS
    assert payload["api_key"] == "sk_wallet"
    assert payload["signup_credit_granted"] is False
    assert any("attach-email" in step for step in payload["next_steps"])
