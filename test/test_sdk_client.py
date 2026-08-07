import pytest

from lium.sdk import Config, Lium, LiumPermissionError


class _Forbidden:
    """A 403 response, usable both directly and as a streaming context manager."""

    ok = False
    status_code = 403
    text = "User is not verified"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_client_sets_version_header(monkeypatch):
    monkeypatch.setattr("lium.sdk.client._get_client_version", lambda: "1.2.3")

    client = Lium(Config(api_key="test"), source="cli")

    assert client.headers["X-API-KEY"] == "test"
    assert client.headers["X-Source"] == "cli"
    assert client.headers["X-Lium-Client-Version"] == "1.2.3"


def test_request_403_raises_permission_error(monkeypatch):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: _Forbidden())
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumPermissionError):
        client._request("GET", "/pods")


def test_logs_403_raises_permission_error(monkeypatch):
    monkeypatch.setattr("lium.sdk.client.requests.get", lambda *a, **kw: _Forbidden())
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumPermissionError):
        list(client.logs("pod-1"))
