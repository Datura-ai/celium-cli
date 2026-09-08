"""The API's error code, hint and request_id reach the caller (DAH-3057): on the SDK exception,
under the CLI's error line, and in the --json envelope. Absent on an older server → nothing added."""

import json

import click
import pytest

from lium.cli.utils import handle_errors
from lium.sdk import Config, Lium, LiumError, LiumPermissionError

ENVELOPE = {
    "success": False,
    "error": {
        "code": "insufficient_balance",
        "message": "Insufficient balance",
        "hint": "Top up at https://lium.io/billing; renting needs a positive balance covering 15 minutes of the node.",
        "request_id": "4f1c9d2e8a7b4c3d9e0f1a2b3c4d5e6f",
    },
    "message": "Insufficient balance",
    "status_code": 403,
}


class _Response:
    ok = False
    text = ""

    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _client(monkeypatch, response):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: response)
    return Lium(Config(api_key="test"))


def test_envelope_fields_land_on_the_exception(monkeypatch):
    client = _client(monkeypatch, _Response(403, ENVELOPE))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("POST", "/executors/x/rent")

    e = raised.value
    assert str(e) == "Permission denied: Insufficient balance"  # unchanged for callers matching on it
    assert (e.code, e.hint, e.request_id) == (
        "insufficient_balance",
        ENVELOPE["error"]["hint"],
        "4f1c9d2e8a7b4c3d9e0f1a2b3c4d5e6f",
    )


def test_request_id_falls_back_to_the_header_when_the_body_has_none(monkeypatch):
    client = _client(monkeypatch, _Response(500, None, {"X-Request-Id": "hdr-0123456789"}))

    with pytest.raises(LiumError) as raised:
        client._request("GET", "/pods")

    assert (raised.value.code, raised.value.hint, raised.value.request_id) == (None, None, "hdr-0123456789")


def test_an_old_server_body_leaves_the_fields_empty(monkeypatch):
    client = _client(monkeypatch, _Response(400, {"success": False, "error": "HTTP error", "message": "Node is not available."}))

    with pytest.raises(LiumError) as raised:
        client._request("POST", "/executors/x/rent")

    assert str(raised.value) == "API error 400: Node is not available."
    assert (raised.value.code, raised.value.hint, raised.value.request_id) == (None, None, None)


def _failing_command(exc):
    @click.command()
    @click.option("--json", "json_output", is_flag=True)
    @handle_errors
    def cmd(json_output):
        raise exc

    return cmd


def test_cli_prints_the_hint_and_the_request_id_under_the_error(capsys):
    exc = LiumError("API error 400: Node is not available.", code="node_unavailable",
                    hint="Pick another node (lium ls) or retry in a minute.", request_id="abc123def456")

    with pytest.raises(SystemExit) as exit_info:
        _failing_command(exc).main([], standalone_mode=False)

    out = capsys.readouterr()
    text = out.out + out.err
    assert exit_info.value.code != 0
    assert "Node is not available." in text
    assert "Pick another node" in text
    assert "request_id: abc123def456" in text


def test_cli_json_envelope_carries_the_server_code_hint_and_request_id(capsys):
    exc = LiumError("API error 400: Node is not available.", code="node_unavailable", hint="Pick another node.",
                    request_id="abc123def456")

    with pytest.raises(SystemExit):
        _failing_command(exc).main(["--json"], standalone_mode=False)

    envelope = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert envelope["error"]["code"] == "node_unavailable"
    assert envelope["error"]["hint"] == "Pick another node."  # the server's hint, not the default for the code
    assert envelope["data"] == {"request_id": "abc123def456"}


def test_cli_without_server_context_prints_only_the_error(capsys):
    with pytest.raises(SystemExit):
        _failing_command(LiumError("API error 400: Node is not available.")).main([], standalone_mode=False)

    out = capsys.readouterr()
    assert "request_id" not in out.out + out.err
