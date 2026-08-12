from types import SimpleNamespace

import pytest

from lium.sdk import Config, Lium, LiumError, LiumPermissionError


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
    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *a, **kw: _Forbidden()
    )
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
            "elapsed_seconds": 12,
            "throughput_bytes_per_second": 20,
            "estimated_remaining_seconds": 38,
        }
    )

    assert restore_log.stage == "RESTORING"
    assert restore_log.restore_mode == "STARTUP"
    assert restore_log.processed_files == 25
    assert restore_log.processed_bytes == 250
    assert restore_log.elapsed_seconds == 12
    assert restore_log.estimated_remaining_seconds == 38


def test_backup_and_restore_lifecycle_methods_use_distinct_endpoints(monkeypatch):
    client = Lium(Config(api_key="test"))
    calls = []

    class Response:
        def json(self):
            return {"success": True}

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    client.backup_cancel("backup-1")
    client.backup_log_delete("backup-1")
    client.restore_cancel("restore-1")

    assert calls == [
        ("POST", "/backup-logs/backup-1/cancel"),
        ("DELETE", "/backup-logs/backup-1"),
        ("POST", "/restore-logs/restore-1/cancel"),
    ]


def test_structured_busy_error_is_readable(monkeypatch):
    class ConflictResponse:
        ok = False
        status_code = 409
        text = ""

        def json(self):
            return {
                "detail": {
                    "code": "BACKUP_STORAGE_BUSY",
                    "message": "Another backup or restore is already running",
                    "active_operation_id": "active-123",
                }
            }

    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *args, **kwargs: ConflictResponse()
    )
    client = Lium(Config(api_key="test"))

    with pytest.raises(
        LiumError, match="Another backup or restore is already running.*active-123"
    ):
        client._request("POST", "/pods/pod-1/backup")
