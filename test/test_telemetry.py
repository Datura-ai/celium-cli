"""Opt-in crash reporting (DAH-2057).

Off by default, on only by the user's hand, and when on it sends the crash and the
command name — not the arguments, not the machine, not the account.
"""

import click
import pytest
import sentry_sdk
from click.testing import CliRunner

from lium.cli import telemetry
from lium.cli.utils import EXIT_GENERAL_ERROR, handle_errors


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_TELEMETRY", raising=False)
    monkeypatch.delenv("LIUM_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("LIUM_SENTRY_DSN", raising=False)
    monkeypatch.setattr(telemetry, "_initialised", False)
    yield
    sentry_sdk.get_global_scope().set_client(None)


def test_off_by_default():
    assert telemetry.enabled() is False


@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)])
def test_environment_variable_decides(monkeypatch, value, expected):
    monkeypatch.setenv("LIUM_TELEMETRY", value)

    assert telemetry.enabled() is expected


def test_config_file_decides_when_the_variable_is_absent():
    from lium.cli.settings import ConfigManager

    ConfigManager().set("telemetry.enabled", "true")
    assert telemetry.enabled() is True

    ConfigManager().set("telemetry.enabled", "false")
    assert telemetry.enabled() is False


def test_enabled_without_a_dsn_still_sends_nothing(monkeypatch):
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setattr(telemetry, "DEFAULT_SENTRY_DSN", "")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("SDK initialised without a DSN"))

    assert telemetry.init("lium up", "0.0.33") is False
    assert telemetry.report(RuntimeError("x")) is False


def test_disabled_never_touches_the_sdk(monkeypatch):
    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("SDK initialised while telemetry is off"))

    assert telemetry.init("lium up", "0.0.33") is False


class ListTransport(sentry_sdk.transport.Transport):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def events(monkeypatch):
    captured = []
    real_init = sentry_sdk.init
    monkeypatch.setenv("LIUM_TELEMETRY", "1")
    monkeypatch.setenv("LIUM_SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: real_init(transport=ListTransport(captured), **kwargs))
    return captured


def _crash_with_secrets_in_scope(pod_name: str, api_key: str):
    home_path = "/Users/renter/.lium/config.ini"
    raise RuntimeError(f"cannot read {home_path} for renter@example.com key {api_key}")


def test_report_sends_the_crash_and_the_command_but_not_the_values(events):
    assert telemetry.init("lium up", "0.0.33") is True
    # built at runtime: the SDK attaches source context lines, so a literal here would show up legitimately
    pod_name = "-".join(["my", "secret", "pod"])
    api_key = "sk_" + "".join(chr(ord("A") + i % 26) for i in range(43))

    @click.command("up")
    @click.argument("pod_name")
    @handle_errors
    def up(pod_name):
        _crash_with_secrets_in_scope(pod_name, api_key)

    result = CliRunner().invoke(up, [pod_name])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Unexpected error" in result.output
    assert telemetry.OPT_IN_HINT not in result.output  # already opted in, no nudge
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["command"] == "up"
    assert event["tags"]["python"] and event["tags"]["os"]
    assert event["release"] == "lium-cli@0.0.33"
    exc = event["exception"]["values"][0]
    assert exc["type"] == "RuntimeError"
    assert exc["value"] == "cannot read ~/.lium/config.ini for [email] key [api-key]"
    frames = exc["stacktrace"]["frames"]
    assert frames, "the stack is the point of the report"
    assert all("vars" not in frame for frame in frames)  # local variables held pod_name and the key
    assert all("/Users/" not in frame.get("abs_path", "") for frame in frames)
    for absent in ("request", "user", "breadcrumbs", "server_name", "modules", "extra"):
        assert absent not in event
    serialised = repr(event)
    assert pod_name not in serialised
    assert api_key[3:] not in serialised


def test_expected_failures_are_not_crashes(events):
    from lium.sdk import LiumError

    assert telemetry.init("lium ps", "0.0.33") is True

    @click.command("ps")
    @handle_errors
    def ps():
        raise LiumError("balance too low")

    result = CliRunner().invoke(ps, [])

    assert result.exit_code != 0
    assert events == []


def test_unexpected_error_mentions_the_opt_in_when_off():
    @click.command("ps")
    @handle_errors
    def ps():
        raise KeyError("gpu_count")

    result = CliRunner().invoke(ps, [])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert telemetry.OPT_IN_HINT in result.output
