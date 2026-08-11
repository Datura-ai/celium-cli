from types import SimpleNamespace

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


def test_restore_uses_pod_safe_default_path(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured = {}
    pod = SimpleNamespace(id="pod-1", default_restore_path="/workspace/restored")

    class Response:
        def json(self):
            return {"success": True}

    def fake_request(method, endpoint, json=None, **kwargs):
        captured.update(method=method, endpoint=endpoint, payload=json)
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    client.restore(pod, backup_id="backup-123")

    assert captured["endpoint"] == "/pods/pod-1/restore"
    assert captured["payload"]["restore_path"] == "/workspace/restored"


def test_restore_log_hydrates_progress_metadata():
    client = Lium(Config(api_key="test"))

    restore_log = client._dict_to_restore_log(
        {
            "id": "restore-1",
            "backup_id": "backup-1",
            "pod_id": "pod-1",
            "status": "IN_PROGRESS",
            "progress": 12.34,
            "created_at": "2026-08-12T12:00:00Z",
            "backup_engine": "RESTIC",
            "restore_mode": "STARTUP",
            "stage": "RESTORING",
            "last_heartbeat_at": "2026-08-12T12:01:00Z",
            "total_files": 100,
            "processed_files": 25,
            "total_bytes": 1_000,
            "processed_bytes": 250,
        }
    )

    assert restore_log.stage == "RESTORING"
    assert restore_log.restore_mode == "STARTUP"
    assert restore_log.processed_files == 25
    assert restore_log.processed_bytes == 250
