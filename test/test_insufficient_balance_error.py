"""A balance refusal is its own exception, carrying the numbers.

A caller that wants to react to "not enough balance" (top up, pick a cheaper
executor, stop a batch) had to grep the message of a generic
`LiumPermissionError`. `LiumInsufficientBalanceError` is a subclass so the old
handlers keep working, and `.required` / `.available` are floats (USD) when the
server said what they are.
"""

import pytest

from lium.sdk import Config, Lium, LiumError, LiumInsufficientBalanceError, LiumPermissionError
from lium.sdk import client as client_module


class _Response:
    ok = False
    text = ""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _client_receiving(monkeypatch, response):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: response)
    return Lium(Config(api_key="k-0123456789abcdef", api_key_source="env:LIUM_API_KEY"))


def test_it_is_a_permission_error_and_a_lium_error():
    assert issubclass(LiumInsufficientBalanceError, LiumPermissionError)
    assert issubclass(LiumInsufficientBalanceError, LiumError)


def test_amounts_default_to_none():
    error = LiumInsufficientBalanceError("nope")

    assert (error.required, error.available) == (None, None)
    assert str(error) == "nope"


def test_a_403_about_balance_raises_the_subclass_with_the_amounts(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {
        "detail": {"message": "Insufficient balance", "required_balance": 12.5, "current_balance": "3"},
    }))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert raised.value.required == 12.5
    assert raised.value.available == 3.0
    assert "Insufficient balance (required $12.50, available $3.00)" in str(raised.value)
    assert "from env:LIUM_API_KEY" in str(raised.value)


def test_amounts_are_none_when_the_server_gives_none(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "insufficient balance"}))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert (raised.value.required, raised.value.available) == (None, None)
    assert "required" not in str(raised.value)


def test_insufficient_funds_wording_counts_as_a_balance_error(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "Insufficient funds", "cost": 4}))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert raised.value.required == 4.0 and raised.value.available is None


def test_other_403s_stay_plain_permission_errors(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "User is not verified", "balance": 99}))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("GET", "/pods")

    assert not isinstance(raised.value, LiumInsufficientBalanceError)
    assert "available" not in str(raised.value)


def test_old_handlers_catching_permission_error_still_work(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "Insufficient balance"}))

    with pytest.raises(LiumPermissionError):
        client._request("POST", "/executors/node-1/rent")


@pytest.mark.parametrize("payload, expected", [
    ({"required": 1, "available": 2}, (1.0, 2.0)),
    ({"data": {"price": "7.25"}}, (7.25, None)),
    ({"detail": {"available_balance": 0}}, (None, 0.0)),
    ({"required": True}, (None, None)),          # booleans are not amounts
    ({"required": "lots"}, (None, None)),
    ("a string body", (None, None)),
    (None, (None, None)),                         # no JSON body at all
])
def test_balance_amounts_search_the_usual_places(payload, expected):
    assert client_module._balance_amounts(_Response(403, payload)) == expected


def test_balance_detail_formats_whatever_is_known():
    assert client_module._balance_detail(12.5, 3) == " (required $12.50, available $3.00)"
    assert client_module._balance_detail(None, 3) == " (available $3.00)"
    assert client_module._balance_detail(None, None) == ""
