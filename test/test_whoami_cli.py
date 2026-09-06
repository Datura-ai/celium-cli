"""`lium whoami`: the first command to run when something is off.

Auth and balance errors already name the key they were raised for; `whoami`
answers the question before the error happens.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.whoami import identity as identity_module
from lium.cli.whoami.identity import collect_identity
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import ExecutorInfo, LiumAuthError, PodInfo, SSHKey

KEY = "sk_abcdef0123456789wxyz"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample"


def _executor(gpu_type="H100", max_cuda=12.8, gpu_name=""):
    return ExecutorInfo(
        id="ex-1", huid="node-1", machine_name="m", gpu_type=gpu_type, gpu_count=1,
        price_per_hour=2.0, price_per_gpu=2.0, location={}, status="active", docker_in_docker=False,
        ip="1.2.3.4", max_cuda_version=max_cuda,
        specs={"gpu": {"details": [{"name": gpu_name or gpu_type}]}},
    )


def _pod(status="RUNNING", ssh_cmd="ssh root@1.2.3.4 -p 20299", executor=None, template=None):
    return PodInfo(
        id="pod-1", name="train", huid="swift-fox-c8", status=status, ssh_cmd=ssh_cmd, ports={},
        created_at="", updated_at="", executor=executor, template=template or {},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


@pytest.fixture
def local_setup(monkeypatch, tmp_path):
    """A key in the environment, an ssh key pair under a temp HOME, ssh and rsync installed."""
    monkeypatch.setenv("LIUM_API_KEY", KEY)
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    key.with_suffix(".pub").write_text(f"{PUB} user@host\n")
    monkeypatch.setattr(identity_module, "local_ssh_key_path", lambda: key)
    monkeypatch.setattr(identity_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    return key


class _FakeLium:
    me_payload = {"id": "acct-1", "email": "user@example.com", "balance": 12.5}
    registered = [SSHKey(id="k1", name="laptop", public_key=f"{PUB} other-comment")]
    me_error = None

    def __init__(self, config=None, *a, **k):
        self.config = config or SimpleNamespace(base_url="https://api.example")
        self.config.base_url = getattr(self.config, "base_url", None) or "https://api.example"

    def me(self):
        if self.me_error:
            raise self.me_error
        return dict(self.me_payload)

    def list_ssh_keys(self):
        return list(self.registered)

    def ps(self):
        return []


@pytest.fixture
def fake_lium(monkeypatch):
    class _Lium(_FakeLium):
        pass

    monkeypatch.setattr(identity_module, "Lium", _Lium)
    return _Lium


# --- identity ------------------------------------------------------------------------------

def test_collect_identity_gathers_everything(local_setup, fake_lium):
    identity = collect_identity()

    assert identity.api_key_fingerprint == "sk_abc…wxyz"
    assert identity.api_key_source == "env:LIUM_API_KEY"
    assert identity.api_reachable is True and identity.api_latency_ms is not None
    assert identity.account_id == "acct-1" and identity.email == "user@example.com"
    assert identity.balance_usd == 12.5
    assert identity.ssh_key_path == str(local_setup)
    assert identity.ssh_public_key_found is True and identity.ssh_key_registered is True
    assert identity.ssh_client == "/usr/bin/ssh" and identity.rsync_client == "/usr/bin/rsync"


def test_collect_identity_without_a_key_stops_before_the_network(monkeypatch, local_setup, fake_lium):
    monkeypatch.delenv("LIUM_API_KEY")
    monkeypatch.setattr(identity_module, "resolve_api_key", lambda: (None, None))

    identity = collect_identity()

    assert identity.api_key_fingerprint == "none" and not identity.has_api_key
    assert identity.api_reachable is None and identity.account_id is None
    assert any("LIUM_API_KEY" in w for w in identity.warnings)


def test_collect_identity_reports_an_unreachable_api_instead_of_raising(local_setup, fake_lium):
    fake_lium.me_error = LiumAuthError("Invalid API key (key sk_abc…wxyz from env:LIUM_API_KEY)")

    identity = collect_identity()

    assert identity.api_reachable is False
    assert "Invalid API key" in identity.api_error
    assert identity.ssh_key_registered is None


def test_ssh_key_registration_compares_key_material_not_comments(local_setup, fake_lium):
    fake_lium.registered = [SSHKey(id="k", name="n", public_key="ssh-ed25519 AAAADifferent me@here")]

    assert collect_identity().ssh_key_registered is False


def test_public_key_material_ignores_missing_or_odd_files(tmp_path):
    assert identity_module.public_key_material(None) is None
    assert identity_module.public_key_material(tmp_path / "nope") is None
    key = tmp_path / "id_rsa"
    key.with_suffix(".pub").write_text("# just a comment\n")
    assert identity_module.public_key_material(key) is None


def test_local_ssh_key_path_prefers_the_configured_one(monkeypatch, tmp_path):
    from lium.cli import settings

    monkeypatch.setattr(settings.config, "get", lambda key: "~/keys/custom" if key == "ssh.key_path" else None)

    assert identity_module.local_ssh_key_path() == Path("~/keys/custom").expanduser()


# --- whoami --------------------------------------------------------------------------------

def test_whoami_prints_the_identity(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["whoami"])

    assert result.exit_code == 0, result.output
    assert "sk_abc…wxyz" in result.output and "env:LIUM_API_KEY" in result.output
    assert "acct-1" in result.output and "$12.50" in result.output
    assert "registered: yes" in result.output
    assert "reachable" in result.output


def test_whoami_json_is_the_identity_dict(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["account_id"] == "acct-1"
    assert payload["api_key_fingerprint"] == "sk_abc…wxyz"
    assert payload["ssh_key_registered"] is True
    assert KEY not in result.output  # never the full key


def test_whoami_without_a_key_exits_configuration_error(monkeypatch, local_setup, fake_lium):
    monkeypatch.setattr(identity_module, "resolve_api_key", lambda: (None, None))

    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.output.splitlines()[0])["api_key_fingerprint"] == "none"


def test_whoami_with_an_unreachable_api_exits_api_error(local_setup, fake_lium):
    fake_lium.me_error = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["whoami"])

    assert result.exit_code == EXIT_API_ERROR
    assert "unreachable: Invalid API key" in result.output
