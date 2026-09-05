"""DAH-2565: stop a rental once it has spent $X.

A rental could be capped by time (--ttl/--until) but not by money. Billing is
price × wall time from creation, so a budget is a deadline; this turns
`--budget USD` into the same scheduled removal `--ttl` uses, and shows spend
against the cap in `lium ps`.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.ps import display as ps_display
from lium.cli.up import command as up_command
from lium.cli.up.budget import MIN_BUDGET_MINUTES, budget_deadline, budget_hours
from lium.cli.utils import EXIT_CONFIGURATION_ERROR
from lium.sdk import Config, ExecutorInfo, Lium, PodInfo
from lium.sdk.utils import parse_api_timestamp, spend_cap_deadline

CREATED = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
CREATED_STR = "2026-09-05T12:00:00Z"
PRICE = 2.50


def _executor(price: float | None = PRICE) -> ExecutorInfo:
    return ExecutorInfo(
        id="exec-1", huid="brave-fox-3a", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
        gpu_count=1, price_per_hour=price, price_per_gpu=price, location={}, specs={},
        status="active", docker_in_docker=False, ip="",
    )


def _pod(created_at: str | None = CREATED_STR, price: float | None = PRICE, removal: str | None = None) -> PodInfo:
    return PodInfo(
        id="pod-1", name="train", status="RUNNING", huid="eager-wolf-aa",
        ssh_cmd="ssh user@pod.example -p 20000", ports={}, created_at=created_at or "",
        updated_at=CREATED_STR, executor=_executor(price) if price is not None else None,
        template={}, removal_scheduled_at=removal, jupyter_installation_status=None, jupyter_url=None,
    )


# --- arithmetic -----------------------------------------------------------------------------


def test_spend_cap_deadline_is_budget_over_price_after_start():
    assert spend_cap_deadline(CREATED, PRICE, 12.50) == CREATED + timedelta(hours=5)


@pytest.mark.parametrize("budget, price", [(0, PRICE), (-1, PRICE), (10, 0), (10, None)])
def test_spend_cap_deadline_rejects_what_is_not_a_cap(budget, price):
    with pytest.raises(ValueError):
        spend_cap_deadline(CREATED, price, budget)


def test_parse_api_timestamp_handles_z_and_naive_forms():
    assert parse_api_timestamp("2026-09-05T12:00:00Z") == CREATED
    assert parse_api_timestamp("2026-09-05T12:00:00") == CREATED
    assert parse_api_timestamp(None) is None
    assert parse_api_timestamp("yesterday") is None


def test_budget_hours_needs_a_price():
    assert budget_hours(12.50, PRICE) == 5
    assert budget_hours(12.50, 0) is None
    assert budget_hours(12.50, None) is None


def test_budget_deadline_is_anchored_on_created_at():
    assert budget_deadline(_pod(), 12.50) == CREATED + timedelta(hours=5)


def test_budget_deadline_falls_back_to_now_without_created_at():
    now = CREATED + timedelta(minutes=3)

    assert budget_deadline(_pod(created_at=None), 12.50, now=now) == now + timedelta(hours=5)


def test_budget_deadline_uses_the_executor_price_seen_before_renting_when_the_pod_has_none():
    assert budget_deadline(_pod(price=None), 12.50, fallback_price=5.0) == CREATED + timedelta(hours=2.5)
    assert budget_deadline(_pod(price=None), 12.50) is None


# --- SDK ------------------------------------------------------------------------------------


def test_sdk_cap_spend_schedules_removal_at_the_deadline(monkeypatch):
    client = Lium(Config(api_key="test"))
    scheduled = {}
    monkeypatch.setattr(client, "schedule_termination", lambda pod, *, termination_time: scheduled.update(t=termination_time) or {})
    far_future_pod = _pod(created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())

    deadline = client.cap_spend(far_future_pod, budget_usd=PRICE * 2)

    assert deadline - parse_api_timestamp(far_future_pod.created_at) == timedelta(hours=2)
    assert scheduled["t"] == deadline.isoformat().replace("+00:00", "Z")


def test_sdk_cap_spend_refuses_an_already_spent_budget(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "schedule_termination", lambda *a, **k: pytest.fail("must not schedule"))

    with pytest.raises(ValueError, match="already spent"):
        client.cap_spend(_pod(), budget_usd=0.01)   # created 2026-09-05, long spent


def test_sdk_cap_spend_needs_created_at_and_a_price():
    client = Lium(Config(api_key="test"))

    with pytest.raises(ValueError, match="created_at"):
        client.cap_spend(_pod(created_at=None), budget_usd=10)
    with pytest.raises(ValueError, match="price"):
        client.cap_spend(_pod(price=None), budget_usd=10)


# --- ps -------------------------------------------------------------------------------------


def test_ps_json_reports_the_spend_cap_when_removal_is_scheduled():
    row = ps_display.compact_pod(_pod(removal="2026-09-05T17:00:00Z"))

    assert row["spend_cap_usd"] == 12.50


def test_ps_json_spend_cap_is_none_without_a_schedule():
    assert ps_display.compact_pod(_pod())["spend_cap_usd"] is None


def test_ps_table_shows_spent_against_the_cap():
    with_cap = ps_display._format_spent(CREATED_STR, "2026-09-05T17:00:00Z", PRICE)
    without = ps_display._format_spent(CREATED_STR, None, PRICE)

    assert with_cap.endswith("/$12.50") and with_cap.startswith("$")
    assert "/" not in without


# --- lium up --budget -----------------------------------------------------------------------


def _run_up(monkeypatch, args, *, price=PRICE, pod=None):
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="H100",
        price_per_hour=price, available_port_count=10, download_speed=1000,
    )
    scheduled = {}

    class _Lium:
        def get_deployment_estimate(self, *a, **k):
            return {}

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            scheduled["rented"] = True
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod": pod or _pod()})

    class _Schedule:
        def execute(self, ctx):
            scheduled["at"] = ctx["termination_time"]
            return ActionResult(ok=True, data={"termination_time": ctx["termination_time"], "hours_until": 1})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", _Wait)
    monkeypatch.setattr(up_command, "ScheduleTerminationAction", _Schedule)
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--no-ssh", *args])
    return result, scheduled


def test_up_budget_schedules_removal_when_the_budget_runs_out(monkeypatch):
    result, scheduled = _run_up(monkeypatch, ["--budget", "12.50"])

    assert result.exit_code == 0, result.output
    assert scheduled["at"] == CREATED + timedelta(hours=5)
    assert "Budget $12.50 at $2.50/h ≈ 5.0h" in result.output
    assert "Spend cap $12.50" in result.output


def test_up_budget_and_ttl_keep_the_earlier_deadline(monkeypatch):
    result, scheduled = _run_up(monkeypatch, ["--budget", "12.50", "--ttl", "1h"])
    assert result.exit_code == 0, result.output
    # --ttl 1h from now is far later than the pod's 2026-09-05 creation + 5 h: the budget wins.
    assert scheduled["at"] == CREATED + timedelta(hours=5)

    result, scheduled = _run_up(monkeypatch, ["--budget", "250000", "--ttl", "1h"])
    assert result.exit_code == 0, result.output
    assert scheduled["at"] - datetime.now(timezone.utc) < timedelta(hours=1, minutes=1)


def test_up_budget_that_buys_under_five_minutes_is_refused_before_renting(monkeypatch):
    tiny = PRICE * (MIN_BUDGET_MINUTES - 1) / 60
    result, scheduled = _run_up(monkeypatch, ["--budget", f"{tiny:.4f}"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "rented" not in scheduled
    assert f"minimum is {MIN_BUDGET_MINUTES} min" in result.output


def test_up_budget_without_a_node_price_is_refused_before_renting(monkeypatch):
    result, scheduled = _run_up(monkeypatch, ["--budget", "10"], price=0)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "rented" not in scheduled


def test_up_budget_must_be_positive():
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--budget", "0"])

    assert result.exit_code == 2
    assert "--budget" in result.output


def test_up_without_budget_schedules_nothing(monkeypatch):
    result, scheduled = _run_up(monkeypatch, [])

    assert result.exit_code == 0, result.output
    assert "at" not in scheduled
