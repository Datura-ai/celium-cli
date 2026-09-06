"""``lium provider node status`` / ``lium mine status`` (DAH-3019): the verification step in
progress with elapsed and time left, or the last run's per-step timeline."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.provider._verification import format_seconds, headline, render_text, step_lines
from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.token_store import TokenStore


def _step(index, name, status, **kw):
    return {"index": index, "check_id": f"c.{index}", "name": name, "status": status, "duration_s": None, "p50_s": None, "estimated": False, **kw}


VERIFYING = {
    "phase": "verifying",
    "current": _step(3, "Bandwidth & GPU proof", "running", duration_s=36.0, p50_s=70.0),
    "elapsed_s": 42.0,
    "eta_s": 70.0,
    "basis": "node:5 runs",
    "steps": [_step(i, n, "pending") for i, n in enumerate(["Upload checks", "Hardware scan", "Bandwidth & GPU proof", "GPU benchmark", "Rental check", "Finalize"], 1)],
    "run": {
        "started_at": "2026-09-06T21:00:08Z",
        "cycle_time": "2026-09-06T21:00:00Z",
        "outcome": "running",
        "reason_code": None,
        "event": None,
        "total_s": 160.0,
        "steps": [
            _step(1, "Upload checks", "passed", duration_s=4.0, p50_s=4.0, estimated=True),
            _step(2, "Hardware scan", "passed", duration_s=21.0, p50_s=21.0, estimated=True),
            _step(3, "Bandwidth & GPU proof", "running", duration_s=36.0, p50_s=70.0),
            _step(4, "GPU benchmark", "pending", p50_s=26.0),
            _step(5, "Rental check", "pending", p50_s=5.0),
            _step(6, "Finalize", "pending", p50_s=1.0),
        ],
    },
    "last_run": None,
    "next_check_expected_at": None,
    "publish_expected_at": None,
}

IDLE_FAILED = {
    "phase": "idle",
    "current": None,
    "elapsed_s": None,
    "eta_s": None,
    "basis": "fleet:212 runs",
    "steps": [],
    "run": None,
    "last_run": {
        "started_at": None,
        "cycle_time": "2026-09-06T20:45:00Z",
        "outcome": "failed",
        "reason_code": "VERIFYX_FAILED_NETWORK_SPEED_TOO_SLOW",
        "event": "Network too slow",
        "total_s": 190.0,
        "steps": [
            _step(1, "Hardware scan", "passed", duration_s=21.0),
            _step(2, "Bandwidth & GPU proof", "failed", duration_s=164.6),
            _step(3, "GPU benchmark", "skipped"),
        ],
    },
    "next_check_expected_at": "2026-09-06T21:15:00Z",
    "publish_expected_at": None,
}


def test_format_seconds():
    assert format_seconds(42.4) == "42 s"
    assert format_seconds(125) == "2 min 05 s"
    assert format_seconds(None) == "—"


def test_headline_while_verifying_reads_step_elapsed_and_eta():
    assert headline(VERIFYING) == "verifying · step 3/6 Bandwidth & GPU proof · 42 s elapsed · ~1 min 10 s left"


def test_step_lines_mark_done_running_and_pending():
    lines = step_lines(VERIFYING)
    assert lines[0] == "  ✓ 1. Upload checks — 4 s (estimated)"
    assert lines[2] == "  … 3. Bandwidth & GPU proof — 36 s so far · typically 1 min 10 s"
    assert lines[3] == "  · 4. GPU benchmark — typically 26 s"


def test_render_text_for_a_failed_last_run():
    text = render_text(IDLE_FAILED)
    assert text.splitlines()[0] == (
        "idle · last run failed (VERIFYX_FAILED_NETWORK_SPEED_TOO_SLOW) in 3 min 10 s (cycle 2026-09-06T20:45:00Z)"
        " · next check around 2026-09-06T21:15:00Z"
    )
    assert "  ✗ 2. Bandwidth & GPU proof — 2 min 45 s" in text
    assert "  – 3. GPU benchmark — not reached" in text
    assert text.splitlines()[-1] == "typical durations from the fleet's last 212 runs"


class _Portal:
    def __init__(self, body):
        self.body = body
        self.gets = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append(path)
        return self.body

    def post(self, *a, **k):  # pragma: no cover
        return {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, *a, **k):  # pragma: no cover
        return {}


@pytest.fixture
def patched_client(monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore):
    def _factory(body):
        portal = _Portal(body)

        def _builder(ctx):
            return ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal)  # type: ignore[arg-type]

        monkeypatch.setattr("lium.cli.provider.node.build_client", _builder)
        return portal

    return _factory


def test_node_status_prints_the_headline_and_steps(patched_client):
    portal = patched_client(VERIFYING)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "status", "e-1"])

    assert result.exit_code == 0, result.output
    assert portal.gets == ["/executors/e-1/verification"]
    assert result.output.splitlines()[0] == "verifying · step 3/6 Bandwidth & GPU proof · 42 s elapsed · ~1 min 10 s left"
    assert "  … 3. Bandwidth & GPU proof — 36 s so far · typically 1 min 10 s" in result.output


def test_node_status_json_returns_the_body(patched_client):
    patched_client(VERIFYING)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "--json", "node", "status", "e-1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["phase"] == "verifying"
    assert payload["data"]["eta_s"] == 70.0


def test_node_status_watch_refreshes_until_interrupted(patched_client, monkeypatch):
    portal = patched_client(VERIFYING)
    calls = {"n": 0}

    def _sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr("lium.cli.provider.node.time.sleep", _sleep)
    monkeypatch.setattr("lium.cli.provider.node.click.clear", lambda: None)

    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "node", "status", "e-1", "--watch", "--interval", "2"])

    assert result.exit_code == 0, result.output
    assert portal.gets == ["/executors/e-1/verification"] * 2  # two refreshes, then Ctrl-C


def test_mine_status_is_the_same_command(patched_client, monkeypatch):
    portal = patched_client(IDLE_FAILED)
    monkeypatch.setenv("LIUM_PROVIDER_HOTKEY", "hk1")

    result = CliRunner().invoke(cli, ["mine", "status", "e-1"])

    assert result.exit_code == 0, result.output
    assert portal.gets == ["/executors/e-1/verification"]
    assert result.output.splitlines()[0].startswith("idle · last run failed (VERIFYX_FAILED_NETWORK_SPEED_TOO_SLOW)")


def test_mine_status_json_after_the_node_id(patched_client, monkeypatch):
    patched_client(IDLE_FAILED)
    monkeypatch.setenv("LIUM_PROVIDER_HOTKEY", "hk1")

    result = CliRunner().invoke(cli, ["mine", "status", "e-1", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip())["data"]["phase"] == "idle"
