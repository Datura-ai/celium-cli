import warnings
from types import SimpleNamespace

import pytest

from lium.sdk import Config, Lium, LiumHostKeyError, PodInfo
from lium.sdk import client as sdk_client


def _pod():
    return PodInfo(
        id="pod-123",
        name="backup-test",
        huid="swift-fox-c8",
        status="RUNNING",
        ssh_cmd="ssh root@38.80.122.244 -p 20299",
        ports={},
        created_at="2026-05-14T00:00:00Z",
        updated_at="2026-05-14T00:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


def test_ssh_connection_falls_back_to_agent_when_key_file_cannot_be_loaded(monkeypatch, tmp_path):
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("encrypted-key")
    connect_calls = []

    def raise_ssh_exception(*args, **kwargs):
        raise sdk_client.paramiko.SSHException("encrypted key")

    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", raise_ssh_exception)
    monkeypatch.setattr(sdk_client.paramiko.RSAKey, "from_private_key_file", raise_ssh_exception)
    monkeypatch.setattr(sdk_client.paramiko.ECDSAKey, "from_private_key_file", raise_ssh_exception)

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def load_host_keys(self, filename):
            self.host_keys_file = filename

        def connect(self, **kwargs):
            connect_calls.append(kwargs)

        def close(self):
            self.closed = True

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    monkeypatch.setenv("HOME", str(tmp_path))

    client = Lium(Config(api_key="test", ssh_key_path=key_path))

    with client.ssh_connection(_pod()) as ssh_client:
        assert isinstance(ssh_client, FakeSSHClient)

    assert connect_calls == [
        {
            "hostname": "38.80.122.244",
            "port": 20299,
            "username": "root",
            "timeout": 30,
            "look_for_keys": False,
            "key_filename": str(key_path),
            "allow_agent": True,
        }
    ]


class _PinningSSHClient(sdk_client.paramiko.SSHClient):
    """SSHClient whose connect() replays paramiko's host-key check against a fake server key."""

    server_key = None
    connects = 0

    def connect(self, hostname, port, **kwargs):
        type(self).connects += 1
        lookup = f"[{hostname}]:{port}"
        known = self._host_keys.lookup(lookup)
        key = type(self).server_key
        if known is None:
            self._policy.missing_host_key(self, lookup, key)
        elif known.get(key.get_name()) != key:
            raise sdk_client.paramiko.BadHostKeyException(lookup, key, known[key.get_name()])


def _install_pinning_client(monkeypatch, tmp_path, server_key):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", lambda *a, **k: object())
    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", _PinningSSHClient)
    _PinningSSHClient.server_key = server_key
    _PinningSSHClient.connects = 0
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    return Lium(Config(api_key="test", ssh_key_path=key_path))


def test_ssh_connection_pins_host_key_on_first_use_and_accepts_it_afterwards(monkeypatch, tmp_path):
    server_key = sdk_client.paramiko.ECDSAKey.generate()
    client = _install_pinning_client(monkeypatch, tmp_path, server_key)
    pod = _pod()
    hosts_file = tmp_path / ".lium" / "known_hosts" / "pod-123"

    with pytest.warns(UserWarning, match="Pinning ecdsa-sha2-nistp256 host key"):
        with client.ssh_connection(pod):
            pass

    assert hosts_file.exists()
    assert (hosts_file.stat().st_mode & 0o777) == 0o600
    saved = sdk_client.paramiko.HostKeys(str(hosts_file)).lookup(f"[{pod.host}]:{pod.ssh_port}")
    assert saved["ecdsa-sha2-nistp256"] == server_key

    # Second connection to the same pod with the same key: no warning, no change.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with client.ssh_connection(pod):
            pass
    assert _PinningSSHClient.connects == 2


def test_ssh_connection_rejects_changed_host_key(monkeypatch, tmp_path):
    server_key = sdk_client.paramiko.ECDSAKey.generate()
    client = _install_pinning_client(monkeypatch, tmp_path, server_key)
    pod = _pod()
    with pytest.warns(UserWarning):
        with client.ssh_connection(pod):
            pass

    _PinningSSHClient.server_key = sdk_client.paramiko.ECDSAKey.generate()
    with pytest.raises(LiumHostKeyError, match="Host key for pod backup-test .* changed") as exc:
        with client.ssh_connection(pod):
            pass
    assert "known_hosts" in str(exc.value)
    assert "LIUM_SSH_INSECURE=1" in str(exc.value)


def test_ssh_connection_known_hosts_are_scoped_per_pod(monkeypatch, tmp_path):
    """A new pod on a recycled host:port must not be reported as a changed key."""
    client = _install_pinning_client(monkeypatch, tmp_path, sdk_client.paramiko.ECDSAKey.generate())
    with pytest.warns(UserWarning):
        with client.ssh_connection(_pod()):
            pass

    other = _pod()
    other.id = "pod-456"
    _PinningSSHClient.server_key = sdk_client.paramiko.ECDSAKey.generate()
    with pytest.warns(UserWarning):
        with client.ssh_connection(other):
            pass
    assert sorted(p.name for p in (tmp_path / ".lium" / "known_hosts").iterdir()) == ["pod-123", "pod-456"]


def test_known_hosts_path_sanitises_pod_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pod = _pod()
    pod.id = "../etc/passwd"
    assert sdk_client.known_hosts_path(pod) == tmp_path / ".lium" / "known_hosts" / ".._etc_passwd"


def test_ssh_insecure_env_restores_auto_add_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    monkeypatch.setattr(sdk_client.paramiko.Ed25519Key, "from_private_key_file", lambda *a, **k: object())
    seen = {}

    class FakeSSHClient:
        def set_missing_host_key_policy(self, policy):
            seen["policy"] = policy

        def load_host_keys(self, filename):
            seen["loaded"] = filename

        def connect(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", FakeSSHClient)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    with client.ssh_connection(_pod()):
        pass

    assert isinstance(seen["policy"], sdk_client.paramiko.AutoAddPolicy)
    assert "loaded" not in seen
    assert not (tmp_path / ".lium" / "known_hosts").exists()


def test_rsync_uses_pinned_known_hosts_unless_insecure(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(sdk_client.subprocess, "run", fake_run)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("key")
    client = Lium(Config(api_key="test", ssh_key_path=key_path))
    pod = _pod()

    client.rsync(pod, local="./out", remote="/workspace/out")
    ssh_opt = calls[0][3]
    hosts_file = tmp_path / ".lium" / "known_hosts" / "pod-123"
    assert "-o StrictHostKeyChecking=accept-new" in ssh_opt
    assert f"-o UserKnownHostsFile={hosts_file}" in ssh_opt
    assert "StrictHostKeyChecking=no" not in ssh_opt
    assert hosts_file.exists()

    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    client.rsync(pod, local="./out", remote="/workspace/out")
    assert "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" in calls[1][3]


@pytest.mark.parametrize("method, verb, path", [
    ("reboot", "POST", "/pods/pod-123/reboot"),
    ("down", "DELETE", "/pods/pod-123"),
])
def test_reboot_and_down_forget_the_pinned_host_key(monkeypatch, tmp_path, method, verb, path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pod = _pod()
    hosts_file = sdk_client.known_hosts_path(pod)
    hosts_file.parent.mkdir(parents=True)
    hosts_file.write_text("[host]:1 ssh-ed25519 AAAA\n")
    seen = []

    class Resp:
        def json(self):
            return {"ok": True}

    def fake_request(self, m, p, **kwargs):
        seen.append((m, p))
        return Resp()

    monkeypatch.setattr(Lium, "_request", fake_request)
    client = Lium(Config(api_key="test"))
    assert getattr(client, method)(pod) == {"ok": True}
    assert seen == [(verb, path)]
    assert not hosts_file.exists()
    getattr(client, method)(pod)  # idempotent when there is nothing to forget
