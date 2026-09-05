"""`lium whoami` and `lium doctor`: the first two commands to run when something is off.

Auth and balance errors already name the key they were raised for; `whoami`
answers the question before the error happens, and `doctor` turns the usual
"why does lium not work on this machine / with this pod" into a checklist an
operator or an agent can read.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.doctor import checks as checks_module
from lium.cli.doctor import command as doctor_module
from lium.cli.whoami import identity as identity_module
from lium.cli.whoami.identity import Identity, collect_identity
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND
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
    monkeypatch.setattr(doctor_module, "Lium", _Lium)
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


# --- doctor checks -------------------------------------------------------------------------

def _identity(**overrides) -> Identity:
    base = dict(
        cli_version="1.0", api_key_fingerprint="sk_abc…wxyz", api_key_source="env:LIUM_API_KEY",
        api_base_url="https://api.example", api_reachable=True, api_latency_ms=120, account_id="a",
        balance_usd=10.0, ssh_key_path="/home/u/.ssh/id_ed25519", ssh_public_key_found=True,
        ssh_key_registered=True, ssh_client="/usr/bin/ssh", rsync_client="/usr/bin/rsync",
    )
    base.update(overrides)
    return Identity(**base)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_identity_checks_all_ok():
    checks = _by_name(checks_module.identity_checks(_identity()))

    assert {c.status for c in checks.values()} == {"ok"}
    assert set(checks) == {"api_key", "api", "balance", "ssh_key", "ssh_client", "rsync_client", "cli_version"}


@pytest.mark.parametrize("overrides, name, status, hint_word", [
    ({"api_key_fingerprint": "none", "api_key_source": None, "api_reachable": None}, "api_key", "fail", "LIUM_API_KEY"),
    ({"api_reachable": False, "api_error": "timeout"}, "api", "fail", "network"),
    ({"balance_usd": 0.0}, "balance", "fail", "lium fund"),
    ({"balance_usd": -0.02}, "balance", "fail", "lium fund"),
    ({"balance_usd": 0.4}, "balance", "warn", "Low"),
    ({"ssh_key_path": None}, "ssh_key", "fail", "ssh-keygen"),
    ({"ssh_public_key_found": False}, "ssh_key", "fail", ".pub"),
    ({"ssh_key_registered": False}, "ssh_key", "fail", "lium up"),
    ({"ssh_key_registered": None}, "ssh_key", "warn", None),
    ({"ssh_client": None}, "ssh_client", "fail", "OpenSSH"),
    ({"rsync_client": None}, "rsync_client", "warn", "rsync"),
])
def test_identity_checks_flag_each_problem_with_a_hint(overrides, name, status, hint_word):
    check = _by_name(checks_module.identity_checks(_identity(**overrides)))[name]

    assert check.status == status
    if hint_word:
        assert hint_word in check.hint


def test_negative_balance_is_shown_as_is():
    check = _by_name(checks_module.identity_checks(_identity(balance_usd=-0.02)))["balance"]

    assert check.detail == "$-0.02"


@pytest.mark.parametrize("template, expected", [
    ({"docker_image_tag": "2.5.1-cuda12.6-cudnn9-devel"}, 12.6),
    ({"docker_image": "daturaai/pytorch:cu128"}, 12.8),
    ({"name": "vLLM CUDA 12.4"}, 12.4),
    ({"docker_image_tag": "latest"}, None),
    ({}, None),
])
def test_template_cuda_version(template, expected):
    assert checks_module.template_cuda_version(template) == expected


@pytest.mark.parametrize("gpu, blackwell", [
    ("B200", True), ("NVIDIA B300", True), ("RTX PRO 6000", True), ("RTX 5090", True),
    ("H100", False), ("H200", False), ("A100", False), ("RTX 4090", False), ("", False),
])
def test_is_blackwell(gpu, blackwell):
    assert checks_module.is_blackwell(gpu) is blackwell


def test_pod_checks_warn_on_blackwell_with_an_old_cuda_template():
    pod = _pod(executor=_executor("B200"), template={"name": "PyTorch", "docker_image_tag": "cu126"})

    checks = _by_name(checks_module.pod_checks(pod, connect=lambda h, p: None))

    assert checks["pod_status"].status == "ok"
    assert checks["pod_ssh"].status == "ok" and "1.2.3.4:20299" in checks["pod_ssh"].detail
    assert checks["template_arch"].status == "warn"
    assert "B200 is Blackwell" in checks["template_arch"].detail and "cu128" in checks["template_arch"].hint


def test_pod_checks_warn_when_the_template_cuda_exceeds_the_driver():
    pod = _pod(executor=_executor("H100", max_cuda=12.4), template={"docker_image_tag": "cu128"})

    check = _by_name(checks_module.pod_checks(pod, connect=lambda h, p: None))["template_arch"]

    assert check.status == "warn" and "12.8" in check.detail and "12.4" in check.detail


def test_pod_checks_ok_when_arch_and_template_match():
    pod = _pod(executor=_executor("H100", max_cuda=12.8), template={"docker_image_tag": "cu126"})

    check = _by_name(checks_module.pod_checks(pod, connect=lambda h, p: None))["template_arch"]

    assert check.status == "ok"


def test_pod_checks_say_nothing_about_arch_when_it_cannot_be_derived():
    pod = _pod(executor=_executor("B200"), template={"docker_image_tag": "latest"})

    assert "template_arch" not in _by_name(checks_module.pod_checks(pod, connect=lambda h, p: None))


def test_pod_checks_report_an_unreachable_ssh_port_and_a_pending_pod():
    pod = _pod(status="PENDING")
    checks = _by_name(checks_module.pod_checks(pod, connect=lambda h, p: "timed out"))

    assert checks["pod_status"].status == "warn"
    assert checks["pod_ssh"].status == "fail" and "timed out" in checks["pod_ssh"].detail

    no_ssh = _by_name(checks_module.pod_checks(_pod(ssh_cmd=None), connect=lambda h, p: None))
    assert no_ssh["pod_ssh"].status == "fail"


def test_tcp_reachable_reports_a_refused_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert checks_module.tcp_reachable("127.0.0.1", port, timeout=1) is not None


# --- doctor command ------------------------------------------------------------------------

def test_doctor_all_ok_exits_zero(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Everything looks good" in result.output
    assert "api_key" in result.output and "ssh_key" in result.output


def test_doctor_json_lists_checks_and_status(local_setup, fake_lium):
    fake_lium.me_payload = {"id": "acct-1", "balance": 0.5}

    result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["status"] == "warn"
    names = {c["name"]: c for c in payload["checks"]}
    assert names["balance"]["status"] == "warn"
    assert payload["identity"]["account_id"] == "acct-1"
    assert payload["pod"] is None


def test_doctor_exits_one_when_a_check_fails(local_setup, fake_lium):
    fake_lium.registered = []

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "not registered" in result.output and "Something needs fixing" in result.output


def test_doctor_with_a_pod_adds_the_pod_checks(local_setup, fake_lium, monkeypatch):
    pod = _pod(executor=_executor("B200"), template={"name": "PyTorch", "docker_image_tag": "cu126"})
    fake_lium.ps = lambda self: [pod]
    monkeypatch.setattr(checks_module, "tcp_reachable", lambda h, p, timeout=5.0: None)

    result = CliRunner().invoke(cli, ["doctor", "train", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = {c["name"]: c for c in payload["checks"]}
    assert payload["pod"] == "swift-fox-c8"
    assert names["pod_ssh"]["status"] == "ok"
    assert names["template_arch"]["status"] == "warn" and payload["status"] == "warn"


def test_doctor_with_an_unknown_pod(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["doctor", "nope"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_doctor_with_a_pod_but_no_api_does_not_try_to_list(local_setup, fake_lium):
    fake_lium.me_error = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["doctor", "train", "--json"])

    assert result.exit_code == 1
    names = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert names["api"]["status"] == "fail" and names["pod"]["status"] == "fail"
