"""One error shape, one exit-code table, and a next step on every failure.

A program driving the CLI needs three things from a failure: a stable code to
branch on, the exit status, and what to do about it. The envelope carries all
three, `LIUM_OUTPUT=json` turns it on without a per-command flag, and the
documented exit-code table is checked against the constants the code uses.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli import utils
from lium.cli.cli import cli
from lium.cli.ps import command as ps_module
from lium.cli.utils import (
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_POD_NOT_FOUND,
    EXIT_SSH_ERROR,
    CliFailure,
    default_hint,
    error_envelope,
)
from lium.sdk import (
    LiumAuthError,
    LiumError,
    LiumInsufficientBalanceError,
    LiumNotFoundError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
)

DOC = Path(__file__).resolve().parent.parent / "docs" / "exit-codes.md"


@pytest.fixture(autouse=True)
def _clean_output_env(monkeypatch):
    monkeypatch.delenv(utils.OUTPUT_ENV, raising=False)


def _run_ps_raising(monkeypatch, error: Exception, args=(), env=None):
    class _RaisingLium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            raise error

    monkeypatch.setattr(ps_module, "Lium", _RaisingLium)
    # without a ~/.lium/config.ini `lium ps` runs the interactive key setup before
    # reaching the client; stub it so the test sees the injected error on a fresh runner
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["ps", *args], env=env)


# --- the envelope -----------------------------------------------------------------

def test_envelope_has_code_message_hint_and_exit_code():
    envelope = error_envelope("pod_not_found", "No pods matching: x", EXIT_POD_NOT_FOUND)

    assert envelope["ok"] is False
    assert set(envelope["error"]) == {"code", "message", "hint", "exit_code"}
    assert envelope["error"]["exit_code"] == EXIT_POD_NOT_FOUND
    assert "lium ps" in envelope["error"]["hint"]


def test_a_failure_may_bring_its_own_hint():
    failure = CliFailure("invalid_arguments", "bad", EXIT_CONFIGURATION_ERROR, hint="pass --ttl 1h")

    assert failure.hint == "pass --ttl 1h"
    assert error_envelope(failure.code, failure.message, failure.exit_code, hint=failure.hint)["error"]["hint"] == "pass --ttl 1h"


def test_a_failure_without_a_hint_gets_one_for_its_code():
    assert CliFailure("confirmation_required", "x", EXIT_CONFIGURATION_ERROR).hint == default_hint("confirmation_required")
    assert "--yes" in default_hint("confirmation_required")


@pytest.mark.parametrize("exit_code", [
    EXIT_GENERAL_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_API_ERROR,
    EXIT_SSH_ERROR, EXIT_POD_NOT_FOUND, EXIT_PERMISSION_DENIED,
])
def test_an_unknown_code_still_gets_a_hint_from_its_exit_code(exit_code):
    """No error leaves without a next step, whatever a command named it."""
    assert default_hint("some_new_command_specific_code", exit_code)


def test_envelope_goes_to_stderr_and_stdout_stays_empty(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumServerError("Server error: 502"), ["--format", "json"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "server_error"
    assert payload["error"]["exit_code"] == EXIT_API_ERROR
    assert payload["error"]["hint"]


def test_json_message_carries_no_human_prefix(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumServerError("Server error: 502"), ["--format", "json"])

    assert json.loads(result.stderr)["error"]["message"] == "Server error: 502"


# --- LIUM_OUTPUT=json -------------------------------------------------------------

def test_output_env_turns_failures_into_json_without_a_flag(monkeypatch):
    result = _run_ps_raising(
        monkeypatch, LiumServerError("Server error: 502"), env={utils.OUTPUT_ENV: "json"}
    )

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "server_error"


def test_output_env_with_another_value_keeps_text(monkeypatch):
    result = _run_ps_raising(
        monkeypatch, LiumServerError("Server error: 502"), env={utils.OUTPUT_ENV: "table"}
    )

    assert result.exit_code == EXIT_API_ERROR
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


# --- classification of SDK errors ---------------------------------------------------

@pytest.mark.parametrize("error, code, exit_code", [
    (LiumAuthError("Invalid API key"), "invalid_api_key", EXIT_CONFIGURATION_ERROR),
    (LiumPermissionError("User is not verified"), "permission_denied", EXIT_PERMISSION_DENIED),
    (LiumInsufficientBalanceError("Insufficient balance", required=4.0, available=1.0),
     "insufficient_balance", EXIT_PERMISSION_DENIED),
    (LiumNotFoundError("Resource not found: /x"), "not_found", EXIT_API_ERROR),
    (LiumRateLimitError("Rate limit exceeded"), "rate_limited", EXIT_API_ERROR),
    (LiumServerError("Server error: 503"), "server_error", EXIT_API_ERROR),
    (LiumError("API error 418: teapot"), "lium_error", EXIT_API_ERROR),
    (ValueError("No API key found. Set LIUM_API_KEY"), "no_api_key", EXIT_CONFIGURATION_ERROR),
    (ValueError("bad value"), "value_error", EXIT_CONFIGURATION_ERROR),
    (RuntimeError("boom"), "unexpected_error", EXIT_GENERAL_ERROR),
])
def test_every_sdk_error_maps_to_a_code_and_exit_status(monkeypatch, error, code, exit_code):
    result = _run_ps_raising(monkeypatch, error, ["--format", "json"])

    assert result.exit_code == exit_code
    assert json.loads(result.stderr)["error"]["code"] == code


def test_insufficient_balance_is_still_a_permission_error():
    """Callers that catch the parent must keep working."""
    error = LiumInsufficientBalanceError("Insufficient balance", required=4.0, available=1.0)

    assert isinstance(error, LiumPermissionError)
    assert (error.required, error.available) == (4.0, 1.0)


# --- human rendering ---------------------------------------------------------------

def test_text_mode_prints_the_hint_under_the_error(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumPermissionError("User is not verified"))

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert "User is not verified" in result.output
    assert "lium balance" in result.output


def test_text_mode_does_not_repeat_a_hint_the_message_already_carries(monkeypatch):
    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            raise CliFailure("confirmation_required", "Confirmation required (re-run with --yes)",
                             EXIT_CONFIGURATION_ERROR, hint="re-run with --yes")

    monkeypatch.setattr(ps_module, "Lium", _Lium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["ps"])

    assert result.output.lower().count("re-run with --yes") == 1


# --- the documentation mirrors the code ------------------------------------------------

def test_exit_code_doc_lists_every_constant_with_its_value():
    doc = DOC.read_text()
    for name, value in vars(utils).items():
        if not name.startswith("EXIT_"):
            continue
        assert re.search(rf"^\|\s*{value}\s*\|\s*`{name}`", doc, re.M), f"{name}={value} missing from {DOC.name}"


def test_exit_code_doc_lists_every_shared_error_code():
    doc = DOC.read_text()
    for code in utils._HINTS_BY_CODE:
        assert f"`{code}`" in doc, f"{code} missing from {DOC.name}"
