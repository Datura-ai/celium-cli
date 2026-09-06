"""DAH-2982: `lium rm` reports what the pod it removes cost.

`rm` lists the pods — with their $/h and start time — right before deleting
them, then printed only `Removed 1 pod(s): <huid>`; renters rebuilt the spend
from observed $/h × wall time afterwards.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.rm import command as rm_module
from lium.cli.rm import display

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _pod(price=0.58, age=timedelta(hours=1, minutes=32), created_at=None, name="train-pod"):
    if created_at is None:
        # The API returns naive UTC timestamps like 2026-09-06T10:28:00.123456.
        created_at = (NOW - age).replace(tzinfo=None).isoformat()
    executor = SimpleNamespace(price_per_hour=price) if price is not None else None
    return SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name=name, executor=executor, created_at=created_at)


def _run_rm(monkeypatch, pod, *args):
    calls = {"rm": [], "scheduled": []}

    class _FakeLium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [pod]

        def rm(self, pod):
            calls["rm"].append(pod.huid)

        def schedule_termination(self, pod, termination_time=None):
            calls["scheduled"].append(termination_time)

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)
    monkeypatch.setattr(rm_module, "datetime", _FrozenDatetime)
    return CliRunner().invoke(cli, ["rm", "train-pod", "-y", *args]), calls


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_pod_spend_is_uptime_times_list_price():
    spend = display.pod_spend(_pod(), now=NOW)

    assert spend == {
        "uptime": "1:32",
        "uptime_hours": 1.53,
        "price_per_hour": 0.58,
        "spent_usd": 0.89,
        "spent_is_estimate": True,
    }


def test_pod_spend_marks_what_it_cannot_know():
    assert display.pod_spend(_pod(price=None), now=NOW)["spent_usd"] is None
    assert display.pod_spend(_pod(price=0.0), now=NOW)["price_per_hour"] is None
    assert display.pod_spend(_pod(created_at="not-a-date"), now=NOW)["uptime"] is None
    assert display.pod_spend(_pod(created_at=""), now=NOW)["uptime_hours"] is None


def test_rm_prints_the_final_spend_line(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod())

    assert result.exit_code == 0, result.output
    assert calls["rm"] == ["eager-wolf-aa"]
    assert "Removed 1 pod(s): eager-wolf-aa" in result.output
    assert "removed train-pod — 1:32 at $0.58/h ≈ $0.89" in result.output


def test_rm_says_when_it_has_no_price(monkeypatch):
    result, _ = _run_rm(monkeypatch, _pod(price=None, age=timedelta(hours=30)))

    assert result.exit_code == 0, result.output
    assert "removed train-pod — 30:00 (no $/h on record)" in result.output


def test_rm_json_carries_the_spend_fields(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod(), "--format", "json")

    assert result.exit_code == 0, result.output
    assert calls["rm"] == ["eager-wolf-aa"]
    payload = json.loads(result.output)
    assert payload["failed"] == []
    assert payload["removed"] == [{
        "id": "pod-uuid-1",
        "huid": "eager-wolf-aa",
        "name": "train-pod",
        "uptime": "1:32",
        "uptime_hours": 1.53,
        "price_per_hour": 0.58,
        "spent_usd": 0.89,
        "spent_is_estimate": True,
    }]


def test_scheduled_removal_reports_scheduled_not_removed(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod(), "--in", "2h", "--format", "json")

    assert result.exit_code == 0, result.output
    assert calls["rm"] == [] and len(calls["scheduled"]) == 1
    payload = json.loads(result.output)
    assert "removed" not in payload
    assert payload["scheduled"][0]["spent_usd"] == 0.89
    assert payload["termination_time"] == calls["scheduled"][0]

    result, _ = _run_rm(monkeypatch, _pod(), "--in", "2h")
    assert "Scheduled removal for 1 pod(s)" in result.output
    assert "≈" not in result.output
