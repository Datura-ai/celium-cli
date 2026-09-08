"""DAH-3047: `Lium.rent` — one call to POST /executors/rent-by-spec when the backend has it,
today's list-filter-rent pick when it does not.

Recorded responses: `GET /version` as the backend answers it before and after DAH-3046, the
rent-by-spec 200/409 bodies, and the `GET /executors` rows the client-side path reads.
"""

import json

import pytest
import responses

from lium.sdk import Config, Lium, LiumError, LiumServerError, RentResult
from lium.sdk import utils as sdk_utils

BASE = "https://lium.io/api"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserKeyForTestingPurposesOnly user@test"
GIB_KB = 1024 * 1024


def _node(node_id, machine_name="NVIDIA H100 NVL", price=1.2, gpu_count=1, cpus=32, download=900.0, country="DE"):
    # a row exactly as GET /executors and GET /nodes carry it
    return {
        "id": node_id,
        "machine_name": machine_name,
        "price_per_gpu": price,
        "gpu_count": gpu_count,
        "available_gpu_count": gpu_count,
        "location": {"country": country, "country_code": country},
        "effective_download_speed_mbps": download,
        "effective_upload_speed_mbps": 500.0,
        "specs": {
            "gpu": {"count": gpu_count, "driver": "535.183.06", "details": [{"name": machine_name, "capacity": 81559}]},
            "cpu": {"count": cpus},
            "ram": {"total": 256 * GIB_KB},
            "hard_disk": {"total": 2000 * GIB_KB, "free": 1500 * GIB_KB},
            "available_port_count": 10,
            "sysbox_runtime": False,
        },
    }


RENTED = {
    "success": True,
    "dry_run": False,
    "pod_id": "pod-uuid-1",
    "template_id": "tpl-default",
    "price_per_hour": 1.2,
    "selected_executor": _node("exec-cheap"),
    "candidates": 3,
    "alternatives_considered": [
        {"id": "exec-dear", "machine_name": "NVIDIA H100 80GB HBM3", "gpu_count": 1, "available_gpu_count": 1,
         "price_per_gpu": 1.9, "price_per_hour": 1.9, "download_mbps": 900.0, "country_code": "US"},
    ],
    "attempts": 2,
}

NO_MATCH = {
    "success": False,
    "code": "no_executor_matches_spec",
    "constraint": "min_cpus",
    "message": "min_cpus=64: none of the 12 node(s) matching the earlier constraints satisfies it; the best on offer is 48",
    "candidates_before": 12,
}


@pytest.fixture
def client(monkeypatch):
    registered = []
    monkeypatch.setattr(Lium, "_ensure_ssh_keys_registered", lambda self, keys, name=None: registered.append(list(keys)))
    lium = Lium(Config(api_key="test"))
    lium.registered = registered
    return lium


def _version(features):
    body = {"started_at": "2026-09-06T00:00:00+00:00", "uptime_seconds": 1}
    if features is not None:
        body["features"] = features
    responses.add(responses.GET, f"{BASE}/version", json=body)


# --- capability detection --------------------------------------------------------------------


@responses.activate
def test_features_are_read_once_from_version(client):
    _version(["rent_by_spec"])

    assert client.supports("rent_by_spec") is True
    assert client.supports("rent_by_spec") is True
    assert client.features() == {"rent_by_spec"}
    assert len(responses.calls) == 1


@responses.activate
def test_an_older_backend_advertises_nothing(client):
    _version(None)  # the /version body before DAH-3046

    assert client.features() == set()
    assert client.supports("rent_by_spec") is False


@responses.activate
def test_an_unreachable_version_means_no_features(client):
    responses.add(responses.GET, f"{BASE}/version", status=503)

    assert client.features() == set()


# --- the server path -----------------------------------------------------------------------


@responses.activate
def test_rent_posts_the_spec_once_and_maps_the_pick(client):
    _version(["rent_by_spec"])
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", json=RENTED)

    result = client.rent(
        gpu_type="H100", gpu_count=1, name="train-1", min_cpus=16, max_price_per_gpu_hour=2.0,
        country="de", min_download_mbps=100, ssh_keys=[KEY], ports=3,
    )

    sent = json.loads(responses.calls[-1].request.body)
    assert sent == {
        "gpu_type": "H100", "gpu_count": 1, "min_cpus": 16, "max_price_per_gpu_hour": 2.0, "country": "de",
        "min_download_mbps": 100,
        "pod_name": "train-1", "template_id": None, "dockerfile_content": None, "volume_id": None,
        "user_public_key": [KEY], "initial_port_count": 3, "enable_volume_encryption": True,
        "backup_log_id": None, "restore_path": None, "dry_run": False,
    }
    assert [c.request.url for c in responses.calls] == [f"{BASE}/version", f"{BASE}/executors/rent-by-spec"]
    assert isinstance(result, RentResult) and result.server_side is True
    assert result.pod == {"id": "pod-uuid-1", "name": "train-1", "status": "PENDING", "executor_id": "exec-cheap"}
    assert result.executor.id == "exec-cheap" and result.executor.gpu_type == "H100"
    assert result.executor.price_per_gpu == 1.2 and result.price_per_hour == 1.2
    assert result.template_id == "tpl-default"
    assert result.candidates == 3 and result.attempts == 2
    assert [a["id"] for a in result.alternatives] == ["exec-dear"]
    assert client.registered == [[KEY]]  # the keys were registered before the rent, as `up` does


@responses.activate
def test_dry_run_rents_nothing_and_registers_no_key(client):
    _version(["rent_by_spec"])
    responses.add(
        responses.POST, f"{BASE}/executors/rent-by-spec",
        json={**RENTED, "dry_run": True, "pod_id": None, "attempts": 0},
    )

    result = client.rent(gpu_type="H100", ssh_keys=[KEY], dry_run=True)

    assert json.loads(responses.calls[-1].request.body)["dry_run"] is True
    assert result.dry_run is True and result.pod is None and result.attempts == 0
    assert result.executor.id == "exec-cheap" and result.price_per_hour == 1.2
    assert client.registered == []


@responses.activate
def test_no_match_raises_with_the_servers_precise_message(client):
    _version(["rent_by_spec"])
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", json=NO_MATCH, status=409)

    with pytest.raises(LiumError, match=r"min_cpus=64: none of the 12 node\(s\) .* the best on offer is 48"):
        client.rent(gpu_type="H100", min_cpus=64, ssh_keys=[KEY])


def test_rent_refuses_contradictory_arguments(client):
    with pytest.raises(ValueError, match="either template_id or dockerfile_content"):
        client.rent(gpu_type="H100", template_id="t", dockerfile_content="FROM x", ssh_keys=[KEY])
    with pytest.raises(ValueError, match="backup_id and restore_path"):
        client.rent(gpu_type="H100", backup_id="b", ssh_keys=[KEY])


@responses.activate
def test_a_rent_is_posted_once_even_when_the_server_fails(client, monkeypatch):
    # a lost response may already have rented a node: the billable POST is never repeated
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    _version(["rent_by_spec"])
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", status=502)
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", json=RENTED)

    with pytest.raises(LiumServerError):
        client.rent(gpu_type="H100", ssh_keys=[KEY])

    assert [c.request.url for c in responses.calls].count(f"{BASE}/executors/rent-by-spec") == 1


@responses.activate
def test_a_dry_run_rents_nothing_so_a_transient_failure_is_retried(client, monkeypatch):
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    _version(["rent_by_spec"])
    responses.add(responses.POST, f"{BASE}/executors/rent-by-spec", status=502)
    responses.add(
        responses.POST, f"{BASE}/executors/rent-by-spec",
        json={**RENTED, "dry_run": True, "pod_id": None, "attempts": 0},
    )

    result = client.rent(gpu_type="H100", ssh_keys=[KEY], dry_run=True)

    assert result.dry_run is True and result.executor.id == "exec-cheap"
    assert [c.request.url for c in responses.calls].count(f"{BASE}/executors/rent-by-spec") == 2


@responses.activate
def test_a_rent_by_spec_backend_refuses_interconnect_before_any_call(client):
    # RentBySpecRequest has no `interconnect` field (lium-platform test_rent_by_spec.py): the server
    # would ignore it and rent a node without checking it, so the SDK refuses instead
    _version(["rent_by_spec"])

    with pytest.raises(LiumError, match="interconnect is not a constraint this backend's rent-by-spec accepts"):
        client.rent(gpu_type="H100", interconnect="nvlink", ssh_keys=[KEY])

    assert [c.request.url for c in responses.calls] == [f"{BASE}/version"]
    assert client.registered == []


# --- the client-side path (older backend) ------------------------------------------------------


def _fleet():
    responses.add(responses.GET, f"{BASE}/machines", json=[{"name": "NVIDIA H100 NVL"}, {"name": "NVIDIA H100 80GB HBM3"}])
    fleet = [
        _node("eight", "NVIDIA H100 80GB HBM3", price=1.0, gpu_count=8),      # cheapest per GPU, wrong size
        _node("dear", "NVIDIA H100 80GB HBM3", price=1.9),
        _node("cheap-slow", "NVIDIA H100 NVL", price=1.2, download=50.0),     # too slow for min_download_mbps
        _node("cheap", "NVIDIA H100 NVL", price=1.2, cpus=48),
        _node("cheap-few-cpus", "NVIDIA H100 NVL", price=1.2, cpus=8),
    ]
    responses.add(responses.GET, f"{BASE}/executors", json=fleet)
    return fleet


@responses.activate
def test_older_backend_lists_filters_picks_the_cheapest_and_rents_by_id(client):
    _version(None)
    _fleet()
    responses.add(responses.POST, f"{BASE}/executors/cheap/rent", json={"id": "pod-uuid-9", "name": "train-1"})

    result = client.rent(
        gpu_type="H100", gpu_count=1, name="train-1", template_id="tpl-1", min_cpus=16, min_download_mbps=100,
        ssh_keys=[KEY],
    )

    assert result.server_side is False and result.attempts == 1
    assert result.executor.id == "cheap" and result.price_per_hour == 1.2
    assert result.candidates == 2 and [a["id"] for a in result.alternatives] == ["dear"]
    assert result.pod == {"id": "pod-uuid-9", "name": "train-1"}
    assert responses.calls[-1].request.url == f"{BASE}/executors/cheap/rent"
    assert json.loads(responses.calls[-1].request.body)["template_id"] == "tpl-1"


@responses.activate
def test_older_backend_dry_run_only_lists(client):
    _version(None)
    _fleet()

    result = client.rent(gpu_type="H100", template_id="tpl-1", ssh_keys=[KEY], dry_run=True)

    assert result.dry_run is True and result.pod is None and result.attempts == 0
    # no floors asked: the three $1.20 nodes tie on price, 900 Mbps beats 50, then the id decides
    assert result.executor.id == "cheap"
    assert not any(c.request.method == "POST" for c in responses.calls)


@responses.activate
def test_older_backend_no_match_names_the_spec_and_what_exists(client):
    _version(None)
    _fleet()

    with pytest.raises(LiumError, match=r"No node matches gpu_type=H100, gpu_count=2\. Available: 1xH100 \$1\.20/h, 1xH100 \$1\.90/h, 8xH100 \$8\.00/h\."):
        client.rent(gpu_type="H100", gpu_count=2, ssh_keys=[KEY])


@responses.activate
def test_older_backend_reads_interconnect_from_the_node_specs(client):
    # the one constraint the server path refuses is checked here: a node that does not report
    # NVLink does not qualify, so the dearer node that does is the pick
    _version(None)
    responses.add(responses.GET, f"{BASE}/machines", json=[{"name": "NVIDIA H100 NVL"}])
    linked = _node("linked", price=1.5)
    linked["specs"]["interconnect"] = {"nvlink": True}
    responses.add(responses.GET, f"{BASE}/executors", json=[_node("plain", price=1.2), linked])

    result = client.rent(gpu_type="H100", interconnect="nvlink", template_id="tpl-1", ssh_keys=[KEY], dry_run=True)

    assert result.executor.id == "linked" and result.candidates == 1
