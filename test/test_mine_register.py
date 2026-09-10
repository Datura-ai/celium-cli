"""`lium mine --register <token>`: the node is posted from what the host reports, then watched until listed.

Origin: design/PROVIDER_BARRIER_PROGRAM.md §B.1 PR-1 (account → first node 34 min median, 20.5 min to listed with no
signal), reports/INVALID_EXECUTOR_ROOTCAUSE.md (hand-typed node values), DAH-3075 (SSH_PUBLIC_PORT left in .env).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt as pyjwt
import pytest
from click.testing import CliRunner

from lium.cli.commands import mine
from lium.cli.commands import mine_register as reg
from lium.provider.errors import ProviderError
from lium.provider.portal_http import PortalHTTP

HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
NOW = 1_800_000_000


def _token(exp: int | None = NOW + 3600, hotkey: str | None = HOTKEY, opt_in: bool | None = True, **extra) -> str:
    # the portal's fastapi_jwt layout: the miner's to_json() under "subject", exp at the top
    subject = {"id": "acct-1", "opt_in_status": opt_in}
    if hotkey is not None:
        subject["miner_hotkey"] = hotkey
    claims = {"subject": subject, "type": "access", "scope": reg.REGISTER_SCOPE, **extra}
    if exp is not None:
        claims["exp"] = exp
    return pyjwt.encode(claims, "not-the-portal-secret-but-long-enough-32b", algorithm="HS256")


# --- token -----------------------------------------------------------------------------------------------------------


def test_parse_register_token_reads_account_expiry_scope_and_opt_in() -> None:
    t = reg.parse_register_token(_token(), now=NOW)
    assert (t.miner_hotkey, t.exp, t.scope, t.opt_in_status) == (HOTKEY, NOW + 3600, "node:register", True)
    assert t.seconds_left(now=NOW) == 3600


def test_parse_register_token_refuses_an_expired_token_before_any_install() -> None:
    with pytest.raises(reg.RegisterError, match="expired at .*Add Node page"):
        reg.parse_register_token(_token(exp=NOW - 1), now=NOW)


def test_parse_register_token_refuses_garbage_and_a_token_without_an_account() -> None:
    with pytest.raises(reg.RegisterError, match="not one the portal issued"):
        reg.parse_register_token("not.a.jwt", now=NOW)
    with pytest.raises(reg.RegisterError, match="names no account"):
        reg.parse_register_token(_token(hotkey=None), now=NOW)
    with pytest.raises(reg.RegisterError, match="names no account"):
        reg.parse_register_token(_token(hotkey="0xnot-ss58"), now=NOW)


def test_parse_register_token_accepts_flat_claims_and_no_expiry() -> None:
    flat = pyjwt.encode({"miner_hotkey": HOTKEY}, "k" * 32, algorithm="HS256")
    t = reg.parse_register_token(flat, now=NOW)
    assert t.miner_hotkey == HOTKEY and t.exp is None and t.seconds_left() is None and t.opt_in_status is None


# --- host facts ------------------------------------------------------------------------------------------------------


def test_parse_nvidia_smi_reports_one_model_its_count_and_vram() -> None:
    out = "NVIDIA L4, 23034\nNVIDIA L4, 23034\nNVIDIA L4, 23034\nNVIDIA L4, 23034\n"
    assert reg.parse_nvidia_smi(out) == reg.GpuInventory(gpu_type="NVIDIA L4", gpu_count=4, vram_gb=22)


def test_parse_nvidia_smi_keeps_the_name_verbatim_including_commas_in_memory_free_output() -> None:
    assert reg.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564").gpu_type == "NVIDIA GeForce RTX 4090"


def test_parse_nvidia_smi_refuses_a_mixed_host_and_an_empty_one() -> None:
    with pytest.raises(reg.RegisterError, match="more than one GPU model .*NVIDIA A10G, NVIDIA L4"):
        reg.parse_nvidia_smi("NVIDIA L4, 23034\nNVIDIA A10G, 23028\n")
    with pytest.raises(reg.RegisterError, match="no GPU"):
        reg.parse_nvidia_smi("\n")


def test_executor_port_comes_from_the_rendered_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("INTERNAL_PORT=8080\nEXTERNAL_PORT=30310 # comment\nSSH_PORT=2200\n")
    assert reg.executor_port(tmp_path) == 30310
    (tmp_path / ".env").write_text("SSH_PORT=2200\n")
    with pytest.raises(reg.RegisterError, match="EXTERNAL_PORT is missing"):
        reg.executor_port(tmp_path)


def test_register_path_renders_ssh_public_port_equal_to_ssh_port(tmp_path: Path) -> None:
    """DAH-3075 negative control for the --register defaults: the template's 2200 never outlives SSH_PORT."""
    (tmp_path / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\nEXTERNAL_PORT=8080\n")
    answers = mine._gather_inputs(HOTKEY, auto=True)
    mine._setup_executor_env(tmp_path, hotkey=answers["hotkey"])
    mine._apply_env_overrides(tmp_path, answers["internal_port"], answers["external_port"], answers["ssh_port"],
                              answers["ssh_public_port"], answers["port_range"])
    env = dict(l.split("=", 1) for l in (tmp_path / ".env").read_text().splitlines() if "=" in l)
    assert env["SSH_PUBLIC_PORT"] == env["SSH_PORT"] == "2200"
    assert reg.executor_port(tmp_path) == 8080


def test_public_ipv4_or_fail_names_the_lookup_failure() -> None:
    assert reg.public_ipv4_or_fail("203.0.113.7") == "203.0.113.7"
    with pytest.raises(reg.RegisterError, match="public IPv4"):
        reg.public_ipv4_or_fail("Unable to determine")


# --- price -----------------------------------------------------------------------------------------------------------


class _Snapshot:
    machine_prices = {"NVIDIA L4": 0.11, "NVIDIA A10 Tensor Core GPU": 0.2, "NVIDIA GeForce RTX 4090": 0.3}


def test_resolve_price_uses_the_portal_default_unless_given(monkeypatch) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())
    assert reg.resolve_price("NVIDIA L4", None) == 0.11
    assert reg.resolve_price("NVIDIA L4", 0.09) == 0.09


def test_resolve_price_names_the_closest_portal_name_for_an_unknown_model(monkeypatch) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())
    with pytest.raises(reg.RegisterError) as e:
        reg.resolve_price("NVIDIA A10G", None)
    assert "does not list the GPU model this host reports ('NVIDIA A10G')" in str(e.value)
    assert "NVIDIA A10 Tensor Core GPU" in str(e.value) and "--gpu-type" in str(e.value)


# --- portal calls ----------------------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Portal:
    """A scripted portal: every request is recorded; responses come from a per-(method, path) queue."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queues: dict[tuple[str, str], list[_Resp]] = {}

    def on(self, method: str, path: str, *responses: _Resp) -> None:
        self.queues.setdefault((method, path), []).extend(responses)

    def request(self, **kw):
        self.calls.append(kw)
        path = kw["url"].split("https://portal.example", 1)[1]
        queue = self.queues[(kw["method"], path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _http(portal: _Portal) -> PortalHTTP:
    return PortalHTTP(base_url="https://portal.example", token_provider=lambda: "TOKEN", session=portal)  # type: ignore[arg-type]


def _listing(node_id: str = "node-1", ip: str = "203.0.113.7", port: int = 8080) -> _Resp:
    return _Resp(200, {"success": True, "data": [
        {"id": node_id, "executor_ip_address": ip, "executor_ip_port": str(port)},
    ], "total": 1})


def test_register_node_posts_with_the_token_and_finds_the_id_without_it() -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id="node-1", already_registered=False)
    post, get = portal.calls
    assert post["headers"]["Authorization"] == "Bearer TOKEN"
    assert post["json"] == {"gpu_type": "NVIDIA L4", "ip_address": "203.0.113.7", "port": 8080,
                            "price_per_gpu": 0.11, "gpu_count": 1}
    assert "Authorization" not in get["headers"]
    assert get["params"] == {"miner_hotkey": HOTKEY, "page": 1, "limit": 100}


def test_register_node_treats_the_duplicate_400_as_already_registered() -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(400, {"detail": "Node already exists with the same ip and port."}))
    portal.on("GET", "/executors", _listing(node_id="old-node"))
    record = reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA L4", gpu_count=1,
                               ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert record == reg.NodeRecord(node_id="old-node", already_registered=True)


@pytest.mark.parametrize(
    ("status", "detail", "expect"),
    [
        (400, "Unsupported gpu type.", "does not list the GPU model"),
        (400, "Price per GPU should be between 0.0 and 0.22.", "refused the node: Price per GPU should be between"),
        (401, "Invalid token", "refused the register token"),
    ],
)
def test_register_node_turns_portal_refusals_into_named_fixes(monkeypatch, status, detail, expect) -> None:
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(status, {"detail": detail}))
    with pytest.raises(reg.RegisterError, match=expect):
        reg.register_node(_http(portal), miner_hotkey=HOTKEY, gpu_type="NVIDIA A10G", gpu_count=1,
                          ip_address="203.0.113.7", port=8080, price_per_gpu=0.11)
    assert len(portal.calls) == 1   # no listing lookup after a refusal


def test_find_node_id_pages_and_matches_on_address_and_port() -> None:
    portal = _Portal()
    page1 = _Resp(200, {"data": [{"id": f"n{i}", "executor_ip_address": "198.51.100.1", "executor_ip_port": "8080"}
                                 for i in range(100)]})
    portal.on("GET", "/executors", page1, _listing(node_id="mine", port=30310))
    assert reg.find_node_id(_http(portal), miner_hotkey=HOTKEY, ip_address="203.0.113.7", port=30310) == "mine"
    assert [c["params"]["page"] for c in portal.calls] == [1, 2]
    portal2 = _Portal()
    portal2.on("GET", "/executors", _Resp(200, {"data": []}))
    assert reg.find_node_id(_http(portal2), miner_hotkey=HOTKEY, ip_address="203.0.113.7", port=8080) is None


def _status(status: str, message: str = "", last_error: dict | None = None) -> _Resp:
    computed = {"status": status, "message": message, "last_error": last_error}
    return _Resp(200, {"id": "node-1", "computed_status": computed})


def test_wait_until_listed_prints_changes_only_and_stops_at_available() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("AVAILABLE"))
    seen: list[str] = []
    now = {"t": 0.0}
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=3600, interval_s=15,
                                  on_change=lambda s, t: seen.append(reg.status_line(s, t)),
                                  sleep=lambda secs: now.__setitem__("t", now["t"] + secs), clock=lambda: now["t"])
    assert final.listed and final.status == "AVAILABLE"
    # three reads (0 s, 15 s, 30 s); the unchanged second reading prints nothing
    assert seen == ["[00:00] VALIDATION_PENDING — Waiting for first validation", "[00:30] AVAILABLE"]
    assert all("Authorization" not in c["headers"] for c in portal.calls)


def test_wait_until_listed_returns_the_named_fix_and_survives_a_read_error() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1",
              _Resp(502, "bad gateway"),
              _status("VALIDATION_FAILED", "Validation failed", {
                  "title": "Sysbox required", "message": "Sysbox required for unrented executor",
                  "source": "Validator", "remediation": "Install the sysbox runtime."}))
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=3600, sleep=lambda _: None)
    assert final.needs_fix
    assert final.fix == "Sysbox required Sysbox required for unrented executor Install the sysbox runtime."
    message, code = reg.result_summary(final, node_url="https://provider.example/nodes/node-1", waited_s=900)
    assert code == 1 and message.startswith("FIX (VALIDATION_FAILED): Sysbox required")


def test_wait_until_listed_times_out_with_the_last_reading() -> None:
    portal = _Portal()
    portal.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting"))
    now = {"t": 0.0}
    final = reg.wait_until_listed(_http(portal), "node-1", timeout_s=250, interval_s=100,
                                  sleep=lambda secs: now.__setitem__("t", now["t"] + secs), clock=lambda: now["t"])
    assert final.status == "VALIDATION_PENDING"
    message, code = reg.result_summary(final, node_url="u", waited_s=250)
    assert code == 2 and message.startswith("Still VALIDATION_PENDING after 4 min")


def test_result_summary_listed_is_exit_zero() -> None:
    message, code = reg.result_summary(reg.NodeStatus("AVAILABLE", "", ""), node_url="u", waited_s=130)
    assert (code, message) == (0, "Node listed (AVAILABLE) after 2 min. u")


def test_portal_web_url_maps_api_host_to_portal_host() -> None:
    assert reg.portal_web_url(None) == "https://provider.lium.io"
    assert reg.portal_web_url("https://provider-api.staging.lium.io/") == "https://provider.staging.lium.io"
    assert reg.portal_web_url("http://localhost:8000") == "http://localhost:8000"


def test_opt_in_fix_only_when_the_token_says_the_account_is_not_connected() -> None:
    off = reg.parse_register_token(_token(opt_in=False), now=NOW)
    assert "not connected to the Lium provider server" in (reg.opt_in_fix(off, None) or "")
    assert reg.opt_in_fix(reg.parse_register_token(_token(opt_in=True), now=NOW), None) is None


# --- the command -----------------------------------------------------------------------------------------------------


def test_mine_register_refuses_an_expired_token_before_touching_the_host(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone"))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) - 5)])
    assert result.exit_code == 1
    assert "expired at" in result.output and calls == []


def test_mine_register_refuses_a_conflicting_hotkey(monkeypatch) -> None:
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600),
                                                    "-k", "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"])
    assert result.exit_code == 1 and "different account" in result.output


def test_mine_register_runs_the_install_then_registers_and_waits(monkeypatch, tmp_path: Path) -> None:
    """End to end through the command with every host action stubbed: the token's account lands in .env,
    --auto is implied, the node is posted from nvidia-smi + .env + public IP, and the wait ends listed."""
    target = tmp_path / "compute-subnet"
    executor_dir = target / "neurons" / "executor"

    def fake_clone(target_dir: Path, branch: str) -> None:
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / ".env.template").write_text(
            "MINER_HOTKEY_SS58_ADDRESS=\nINTERNAL_PORT=8080\nEXTERNAL_PORT=8080\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n")

    monkeypatch.setattr(mine, "_clone_or_update_repo", fake_clone)
    for name in ("_install_executor_tools", "_check_prereqs", "_start_executor", "_validate_executor",
                 "_check_ports_free"):
        monkeypatch.setattr(mine, name, lambda *a, **k: None)

    class _Pull:
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(mine, "_start_preflight_pull", lambda: _Pull())
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ("NVIDIA L4, 23034\n", "") if "nvidia-smi" in cmd else ("", ""))
    monkeypatch.setattr(mine, "_get_public_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())

    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("VALIDATION_PENDING", "Waiting for first validation"),
              _status("AVAILABLE"))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    result = CliRunner().invoke(
        mine.mine_command,
        ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target),
         "--portal-url", "https://provider-api.example", "--wait", "5"],
    )
    assert result.exit_code == 0, result.output
    env = dict(l.split("=", 1) for l in (executor_dir / ".env").read_text().splitlines() if "=" in l)
    assert env["MINER_HOTKEY_SS58_ADDRESS"] == HOTKEY and env["SSH_PUBLIC_PORT"] == env["SSH_PORT"]
    post = next(c for c in portal.calls if c["method"] == "POST")
    assert post["json"] == {"gpu_type": "NVIDIA L4", "ip_address": "203.0.113.7", "port": 8080,
                            "price_per_gpu": 0.11, "gpu_count": 1}
    assert "Node added: 1×NVIDIA L4 (22 GB) at 203.0.113.7:8080, $0.11/GPU/h" in result.output
    assert "https://provider.example/nodes/node-1" in result.output
    assert "VALIDATION_PENDING" in result.output and "Node listed (AVAILABLE)" in result.output


def _wait_no_sleep(http, node_id, **kw):
    kw.setdefault("sleep", lambda _: None)
    return _orig_wait(http, node_id, **kw)


_orig_wait = reg.wait_until_listed


def test_mine_register_exit_one_on_a_named_fix_and_zero_with_wait_zero(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "compute-subnet"
    executor_dir = target / "neurons" / "executor"

    def fake_clone(target_dir: Path, branch: str) -> None:
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\nEXTERNAL_PORT=8080\nSSH_PORT=2200\n")

    monkeypatch.setattr(mine, "_clone_or_update_repo", fake_clone)
    for name in ("_install_executor_tools", "_check_prereqs", "_start_executor", "_validate_executor", "_check_ports_free"):
        monkeypatch.setattr(mine, name, lambda *a, **k: None)

    class _Pull:
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(mine, "_start_preflight_pull", lambda: _Pull())
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ("NVIDIA L4, 23034\n", ""))
    monkeypatch.setattr(mine, "_get_public_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)

    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", _status("OFFLINE", "Node not responding to ping."))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    args = ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target), "--wait", "1"]
    result = CliRunner().invoke(mine.mine_command, args)
    assert result.exit_code == 1 and "FIX (OFFLINE): Node not responding to ping." in result.output

    # --wait 0: registered, no waiting, exit 0
    result = CliRunner().invoke(mine.mine_command, args[:-2] + ["--wait", "0"])
    assert result.exit_code == 0 and "Waiting for the validator" not in result.output


def test_mine_register_unknown_gpu_is_a_named_fix_and_nothing_is_posted(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "compute-subnet"
    executor_dir = target / "neurons" / "executor"

    def fake_clone(target_dir: Path, branch: str) -> None:
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / ".env.template").write_text("MINER_HOTKEY_SS58_ADDRESS=\nEXTERNAL_PORT=8080\nSSH_PORT=2200\n")

    monkeypatch.setattr(mine, "_clone_or_update_repo", fake_clone)
    for name in ("_install_executor_tools", "_check_prereqs", "_start_executor", "_validate_executor", "_check_ports_free"):
        monkeypatch.setattr(mine, name, lambda *a, **k: None)

    class _Pull:
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(mine, "_start_preflight_pull", lambda: _Pull())
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ("NVIDIA A10G, 23028\n", ""))
    monkeypatch.setattr(mine, "_get_public_ip", lambda: "203.0.113.7")
    monkeypatch.setattr(reg, "fetch_shared_config", lambda: _Snapshot())
    portal = _Portal()
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = CliRunner().invoke(mine.mine_command, ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)])
    assert result.exit_code == 1
    assert "does not list the GPU model this host reports ('NVIDIA A10G')" in result.output
    assert "NVIDIA A10 Tensor Core GPU" in result.output and portal.calls == []
