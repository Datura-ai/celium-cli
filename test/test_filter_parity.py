"""Parity and behaviour tests for the DAH-2506 node filters.

The accepted-ID sets asserted here are the same ones
``lium-io-frontend/src/contexts/browseFilterPredicates.test.ts`` asserts against the
mirrored fixture, so a predicate that drifts on one side fails on the other.
"""

from __future__ import annotations

import json
import pathlib

from lium.sdk import Config, Lium
from lium.sdk import filters
from lium.cli.ls.display import _add_table_columns, _sort_key_factory, compact_executor

_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "filter_executors.json"


def _rows() -> list[dict]:
    """The mirrored fixture, plus the API envelope fields it omits for brevity."""
    rows = json.loads(_FIXTURE_PATH.read_text())
    augmented = []
    for row in rows:
        d = dict(row)
        d.setdefault("executor_ip_address", "1.2.3.4")
        d.setdefault("status", "available")
        augmented.append(d)
    return augmented


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _client(monkeypatch) -> Lium:
    client = Lium(Config(api_key="test-key"))
    rows = _rows()
    monkeypatch.setattr(client, "_request", lambda method, endpoint, **kwargs: _Response(rows))
    return client


def _ids(client, **kwargs) -> list[str]:
    return [e.id for e in client.ls(**kwargs)]


ALL_IDS = [
    "split-cheap",
    "full-mid",
    "unscored-single",
    "spot-cheap-noports",
    "secure-noports",
    "split-bigvram",
    "tierless",
    "low-reliability",
    "unscored-pricey",
    "split-mid",
    "single-cheap",
    "no-location",
    "fully-rented",
]


# ---------------------------------------------------------------------------
# Mapping (US-010)
# ---------------------------------------------------------------------------


def test_new_fields_are_mapped(monkeypatch):
    """_dict_to_executor_info drops unknown keys, so the new fields must be mapped explicitly."""
    by_id = {e.id: e for e in _client(monkeypatch).ls()}

    assert by_id["split-cheap"].reliability_score == 99
    assert by_id["split-cheap"].uptime_in_minutes == 43200
    assert by_id["unscored-single"].reliability_score is None


def test_absent_fields_map_to_none():
    """A payload without the new keys yields None, not a crash or a default."""
    client = Lium(Config(api_key="test-key"))
    executor = client._dict_to_executor_info(
        {
            "id": "bare",
            "machine_name": "NVIDIA H100 SXM 8x",
            "executor_ip_address": "1.2.3.4",
            "price_per_gpu": 1.0,
            "specs": {"gpu": {"count": 8, "details": [{"name": "H100", "capacity": 81920}]}},
        }
    )

    assert executor.reliability_score is None
    assert executor.uptime_in_minutes is None


def test_vram_properties(monkeypatch):
    """vram_gb / vram_total_gb derive from the first GPU's capacity in MiB."""
    by_id = {e.id: e for e in _client(monkeypatch).ls()}

    assert by_id["split-cheap"].vram_gb == 80
    assert by_id["split-cheap"].vram_total_gb == 640
    assert by_id["single-cheap"].vram_gb == 24
    assert by_id["single-cheap"].vram_total_gb == 24


# ---------------------------------------------------------------------------
# PREDICATE PARITY — the three subtle predicates (US-013)
# ---------------------------------------------------------------------------


def test_parity_tier(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, tier="any") == ALL_IDS
    assert _ids(client, tier="secure") == [
        "split-cheap",
        "full-mid",
        "secure-noports",
        "split-bigvram",
        "unscored-pricey",
        "single-cheap",
        "no-location",
    ]
    assert _ids(client, tier="spot") == [
        "unscored-single",
        "spot-cheap-noports",
        "low-reliability",
        "split-mid",
    ]


def test_parity_min_reliability_keeps_unscored_by_default(monkeypatch):
    """A missing score means "too new to measure", so those nodes survive by default."""
    client = _client(monkeypatch)

    assert _ids(client, min_reliability=90) == [
        "split-cheap",
        "full-mid",
        "unscored-single",
        "secure-noports",
        "split-bigvram",
        "tierless",
        "unscored-pricey",
        "split-mid",
        "single-cheap",
        "no-location",
    ]


def test_parity_min_reliability_scored_only(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, min_reliability=90, include_unscored=False) == [
        "split-cheap",
        "full-mid",
        "secure-noports",
        "split-bigvram",
        "tierless",
        "split-mid",
        "single-cheap",
        "no-location",
    ]
    assert _ids(client, min_reliability=95, include_unscored=False) == [
        "split-cheap",
        "full-mid",
        "split-bigvram",
        "split-mid",
        "single-cheap",
    ]


def test_parity_max_price_per_gpu(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, max_price_per_gpu=2.5) == [
        "split-cheap",
        "unscored-single",
        "spot-cheap-noports",
        "tierless",
        "low-reliability",
        "single-cheap",
    ]


def test_parity_max_price_total_whole_node(monkeypatch):
    """With no --count, total mode prices the whole node."""
    client = _client(monkeypatch)

    assert _ids(client, max_price_total=5) == [
        "unscored-single",
        "spot-cheap-noports",
        "low-reliability",
        "single-cheap",
        # A node with nothing left to rent costs nothing, so it clears every budget.
        # Degenerate, but both implementations must agree on it.
        "fully-rented",
    ]


def test_parity_max_price_total_prices_the_split(monkeypatch):
    """The bug this rework exists to fix: a $2.50/GPU node rentable one GPU at a time
    must survive a $5 budget, because $2.50 is what the renter would actually pay."""
    client = _client(monkeypatch)

    ids = _ids(client, gpu_count=1, widen_for_splitting=True, max_price_total=5)

    assert "split-cheap" in ids
    assert "full-mid" not in ids  # $4/GPU × 8 = $32 and it cannot be split


def test_parity_max_price_total_with_a_wanted_count(monkeypatch):
    """Exact twin of "total mode with a 1x selection" in browseFilterPredicates.test.ts.

    Applies the price predicate ALONE, because Lium.ls cannot express that — routing
    through ls would also apply the GPU-count filter and compare a different set.
    """
    executors = _client(monkeypatch).ls()

    accepted = [e.id for e in executors if filters.matches_max_price(e, 5, "total", 1)]

    assert accepted == [
        "split-cheap",
        "unscored-single",
        "spot-cheap-noports",
        "low-reliability",
        "single-cheap",
        "fully-rented",
    ]


def test_a_node_with_no_free_gpus_is_priced_at_zero(monkeypatch):
    """$9/GPU × 8 physical GPUs would be $72. Availability, not the card count, sets it."""
    fully_rented = {e.id: e for e in _client(monkeypatch).ls()}["fully-rented"]

    assert filters.display_gpu_count(fully_rented, None) == 0
    assert filters.matches_max_price(fully_rented, 5, "total") is True
    assert filters.matches_max_price(fully_rented, 5, "gpu") is False
    assert fully_rented.vram_total_gb is None


def test_price_modes_disagree_for_a_whole_node_machine(monkeypatch):
    """$4/GPU × 8 GPUs passes a $5 per-GPU limit and fails a $5 total limit."""
    client = _client(monkeypatch)

    assert "full-mid" in _ids(client, max_price_per_gpu=5)
    assert "full-mid" not in _ids(client, max_price_total=5)


# ---------------------------------------------------------------------------
# The remaining predicates (US-010/US-011)
# ---------------------------------------------------------------------------


def test_min_uptime(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, min_uptime_minutes=7 * 1440) == [
        "split-cheap",
        "full-mid",
        "secure-noports",
        "split-bigvram",
        "unscored-pricey",
        "split-mid",
        "single-cheap",
    ]


def test_min_vram_and_total(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, min_vram_gb=141) == ["split-bigvram"]
    assert _ids(client, min_vram_total_gb=640) == [
        "split-cheap",
        "full-mid",
        "split-bigvram",
        "unscored-pricey",
        "no-location",
    ]


def test_min_ports_excludes_unknown_port_counts(monkeypatch):
    """An unknown port count cannot satisfy a minimum, so those nodes drop out."""
    client = _client(monkeypatch)

    ids = _ids(client, min_ports=8)

    assert "spot-cheap-noports" not in ids
    assert "secure-noports" not in ids
    assert _ids(client, min_ports=32) == ["split-cheap", "unscored-single", "unscored-pricey", "split-mid"]


def test_countries_are_case_insensitive_iso_codes(monkeypatch):
    client = _client(monkeypatch)

    assert _ids(client, countries=["us"]) == ["split-cheap", "full-mid", "tierless", "unscored-pricey"]
    assert _ids(client, countries=["DE", "FR"]) == [
        "unscored-single",
        "spot-cheap-noports",
        "secure-noports",
        "split-mid",
        "single-cheap",
    ]
    assert "no-location" not in _ids(client, countries=["US"])


def test_bare_ls_is_unfiltered(monkeypatch):
    """Backward compatibility: every new argument defaults to "do not filter"."""
    assert _ids(_client(monkeypatch)) == ALL_IDS


# ---------------------------------------------------------------------------
# CLI surface (US-011)
# ---------------------------------------------------------------------------


def test_compact_executor_exposes_the_filter_fields(monkeypatch):
    """`lium ls --format json` must expose what the filters act on."""
    executor = {e.id: e for e in _client(monkeypatch).ls()}["split-cheap"]

    payload = compact_executor(executor, is_pareto=False, index=1)

    assert payload["available_gpu_count"] == 8
    assert payload["reliability_score"] == 99
    assert payload["uptime_in_minutes"] == 43200


def test_sort_download_uses_effective_speed():
    """Regression: a duplicate "download" key used to shadow this with a raw specs value."""

    class _Exe:
        def __init__(self, download, raw):
            self.effective_download_speed_mbps = download
            self.specs = {"network": {"download_speed": raw}}

        @property
        def download_speed(self):
            return self.effective_download_speed_mbps or 0.0

    key = _sort_key_factory("download")
    fast_effective = _Exe(download=1000, raw=1)
    slow_effective = _Exe(download=10, raw=9999)

    assert key(fast_effective) < key(slow_effective)


def test_table_shows_both_price_columns():
    """--sort price_total has to sort a number the table actually prints."""
    from rich.table import Table

    table = Table()
    _add_table_columns(table)
    headers = [column.header for column in table.columns]

    assert "$/GPU·h" in headers
    assert "$/h" in headers


# ---------------------------------------------------------------------------
# ls / up agreement (US-012)
# ---------------------------------------------------------------------------


def test_up_and_ls_select_from_the_same_candidate_set(monkeypatch):
    """`lium up --count N` used to compare gpu_count while `lium ls --count N` compared
    available_gpu_count widened for splitting, so the two could be disjoint."""
    from lium.cli.up.actions import ResolveExecutorAction

    client = _client(monkeypatch)
    monkeypatch.setattr("lium.cli.ls.command.store_sorted_executors", lambda executors: executors)

    ls_ids = _ids(client, gpu_count=2, widen_for_splitting=True)
    assert ls_ids, "fixture must offer at least one 2-GPU candidate"

    captured: dict = {}
    original_ls = client.ls

    def recording_ls(**kwargs):
        result = original_ls(**kwargs)
        captured["ids"] = [e.id for e in result]
        return result

    monkeypatch.setattr(client, "ls", recording_ls)

    result = ResolveExecutorAction().execute({"lium": client, "count": 2})

    assert result.ok, result.error
    assert captured["ids"] == ls_ids
    assert result.data["executor"].id in ls_ids


# ---------------------------------------------------------------------------
# CLI flag wiring (US-011) — a renamed ctx key turns a flag into a silent no-op,
# so assert the values that actually reach the SDK.
# ---------------------------------------------------------------------------


def _invoke_ls(monkeypatch, args: list[str]) -> dict:
    """Run `lium ls <args>` against a stub SDK and return the kwargs it received."""
    from click.testing import CliRunner
    from lium.cli.ls import command as ls_module

    captured: dict = {}

    class _StubLium:
        def ls(self, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(ls_module, "Lium", lambda *args, **kwargs: _StubLium())

    result = CliRunner().invoke(ls_module.ls_command, [*args, "--format", "json"])
    assert result.exit_code == 0, result.output

    return captured


def test_ls_flags_reach_the_sdk(monkeypatch):
    captured = _invoke_ls(
        monkeypatch,
        [
            "--tier", "spot",
            "--min-reliability", "90",
            "--min-vram", "80",
            "--min-vram-total", "640",
            "--max-price", "12.5",
            "--max-price-gpu", "3",
            "--ports", "32",
            "--min-cuda", "12.4",
        ],
    )

    assert captured["tier"] == "spot"
    assert captured["min_reliability"] == 90
    assert captured["min_vram_gb"] == 80
    assert captured["min_vram_total_gb"] == 640
    assert captured["max_price_total"] == 12.5
    assert captured["max_price_per_gpu"] == 3
    assert captured["min_ports"] == 32
    assert captured["min_cuda_version"] == 12.4


def test_ls_min_uptime_is_converted_from_days_to_minutes(monkeypatch):
    assert _invoke_ls(monkeypatch, ["--min-uptime", "7"])["min_uptime_minutes"] == 7 * 1440


def test_ls_scored_only_flips_include_unscored(monkeypatch):
    assert _invoke_ls(monkeypatch, ["--min-reliability", "90"])["include_unscored"] is True
    assert _invoke_ls(monkeypatch, ["--min-reliability", "90", "--scored-only"])["include_unscored"] is False


def test_ls_country_is_repeatable(monkeypatch):
    assert _invoke_ls(monkeypatch, ["--country", "US", "--country", "DE"])["countries"] == ["US", "DE"]
    # No --country at all must mean "do not filter", not "match the empty list".
    assert _invoke_ls(monkeypatch, [])["countries"] is None


# ---------------------------------------------------------------------------
# `lium up` rents what was asked for (US-012)
# ---------------------------------------------------------------------------


def test_up_rents_only_the_requested_gpu_count(monkeypatch):
    """The API rents every available GPU when the payload omits gpu_count, so
    `up --count 1` on a splittable 8-GPU node would hand over — and bill — all eight."""
    from lium.cli.up.actions import RentPodAction

    captured: dict = {}

    class _StubLium:
        def up(self, **kwargs):
            captured.update(kwargs)
            return {"id": "pod-1"}

    executor = _client(monkeypatch).ls()[0]  # split-cheap: 8 available, rentable from 1
    result = RentPodAction().execute(
        {"lium": _StubLium(), "executor": executor, "template": None, "name": "p", "count": 1}
    )

    assert result.ok, result.error
    assert captured["gpu_count"] == 1


def test_up_without_a_count_rents_the_whole_node(monkeypatch):
    """No --count means "give me the node", which the API expresses as gpu_count=None."""
    from lium.cli.up.actions import RentPodAction

    captured: dict = {}

    class _StubLium:
        def up(self, **kwargs):
            captured.update(kwargs)
            return {"id": "pod-1"}

    executor = _client(monkeypatch).ls()[0]
    RentPodAction().execute({"lium": _StubLium(), "executor": executor, "template": None, "name": "p"})

    assert captured["gpu_count"] is None


def test_sdk_up_sends_gpu_count_in_the_rent_payload(monkeypatch):
    """Guards the whole chain: a missing payload key is what causes the overbilling."""
    client = Lium(Config(api_key="test-key"))
    captured: dict = {}

    def fake_request(method, endpoint, **kwargs):
        if endpoint.endswith("/rent"):
            captured.update(kwargs.get("json") or {})
            return _Response({"id": "pod-1"})
        return _Response({})

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(client, "get_executor", lambda executor_id: _client(monkeypatch).ls()[0])
    monkeypatch.setattr(client, "_ensure_ssh_keys_registered", lambda *args, **kwargs: None)

    client.up(executor_id="split-cheap", template_id="tpl-1", ssh_keys=["ssh-rsa AAAA"], gpu_count=2)

    assert captured["gpu_count"] == 2


def test_total_price_column_agrees_with_the_max_price_filter(monkeypatch):
    """The $/h column and --max-price must quote the same number, which is what the
    renter would actually pay — not price_per_gpu × every physically installed GPU."""
    by_id = {e.id: e for e in _client(monkeypatch).ls()}

    # 8 installed, 0 free, $9/GPU. The physical count would print $72.00 while the filter
    # compared $0.00.
    assert by_id["fully-rented"].price_per_hour == 0
    assert by_id["split-cheap"].price_per_hour == 20.0
