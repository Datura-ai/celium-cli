"""`lium top`: GPU utilisation of pods without opening a shell on each.

An agent that has started a job needs to know whether the GPUs are busy. It
should be one command, work across every pod, be pollable, and produce JSON
it can act on. One unreachable pod must not hide the readings of the others.
"""

import json
from typing import List

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.top import command as top_module
from lium.cli.top import display
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND
from lium.sdk import GpuStats, LiumError, PodInfo


def _pod(pod_id: str, name: str, huid: str) -> PodInfo:
    return PodInfo(
        id=pod_id, name=name, huid=huid, status="RUNNING", ssh_cmd="ssh root@1.2.3.4 -p 22",
        ports={}, created_at="", updated_at="", executor=None, template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


A = _pod("pod-a", "train", "swift-fox-c8")
B = _pod("pod-b", "eval", "brave-lion-11")

GPUS = [
    GpuStats(index=0, name="NVIDIA H100 80GB HBM3", utilization_pct=97.0, memory_used_mib=61440.0,
             memory_total_mib=81559.0, temperature_c=61.0, power_draw_w=612.3),
    GpuStats(index=1, name="NVIDIA H100 80GB HBM3", utilization_pct=0.0, memory_used_mib=0.0,
             memory_total_mib=81559.0, temperature_c=31.0, power_draw_w=None),
]


def _install(monkeypatch, pods: List[PodInfo], stats=None, failing=()):
    calls = []

    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            return pods

        def gpu_stats(self, pod, **kwargs):
            calls.append(pod.huid)
            if pod.huid in failing:
                raise LiumError(f"nvidia-smi failed on pod {pod.name}")
            return stats or GPUS

    monkeypatch.setattr(top_module, "Lium", _Lium)
    return calls


def test_top_one_pod_prints_a_row_per_gpu_and_a_summary(monkeypatch):
    calls = _install(monkeypatch, [A, B])

    result = CliRunner().invoke(cli, ["top", "train"])

    assert result.exit_code == 0, result.output
    assert calls == ["swift-fox-c8"]
    assert "97%" in result.output and "60.0/79.6 GiB (75%)" in result.output
    assert "612 W" in result.output and "61°C" in result.output
    assert "2 GPUs on 1 pod, avg util 48%, 1 idle" in result.output
    assert "Pod" not in result.output.splitlines()[0]  # no pod column for a single pod


def test_top_all_samples_every_pod_and_labels_the_rows(monkeypatch):
    calls = _install(monkeypatch, [A, B])

    result = CliRunner().invoke(cli, ["top", "--all"])

    assert result.exit_code == 0, result.output
    assert sorted(calls) == ["brave-lion-11", "swift-fox-c8"]
    assert "train" in result.output and "eval" in result.output
    assert "4 GPUs on 2 pods" in result.output


def test_top_json_is_one_object_with_to_dict_gpus(monkeypatch):
    _install(monkeypatch, [A, B])

    result = CliRunner().invoke(cli, ["top", "train,eval", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["sampled_at"]
    assert [p["huid"] for p in payload["pods"]] == ["swift-fox-c8", "brave-lion-11"]
    gpu = payload["pods"][0]["gpus"][0]
    assert gpu["utilization_pct"] == 97.0 and gpu["memory_pct"] == 75.3
    assert payload["pods"][0]["error"] is None


def test_top_keeps_going_when_one_pod_fails(monkeypatch):
    _install(monkeypatch, [A, B], failing={"brave-lion-11"})

    result = CliRunner().invoke(cli, ["top", "--all", "--format", "json"])

    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["ok"] is False
    assert payload["pods"][0]["gpus"] and payload["pods"][1]["gpus"] == []
    assert "nvidia-smi failed" in payload["pods"][1]["error"]


def test_top_failed_pod_is_a_row_in_the_table(monkeypatch):
    _install(monkeypatch, [A, B], failing={"brave-lion-11"})

    result = CliRunner().invoke(cli, ["top", "--all"])

    assert result.exit_code == 1
    assert "nvidia-smi failed on pod eval" in result.output
    assert "97%" in result.output


def test_top_without_a_target_explains_itself(monkeypatch):
    _install(monkeypatch, [A])

    result = CliRunner().invoke(cli, ["top"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--all" in result.output


def test_top_unknown_pod_exits_pod_not_found(monkeypatch):
    _install(monkeypatch, [A])

    result = CliRunner().invoke(cli, ["top", "nope"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_top_all_with_no_pods(monkeypatch):
    _install(monkeypatch, [])

    result = CliRunner().invoke(cli, ["top", "--all"])

    assert result.exit_code == EXIT_POD_NOT_FOUND


def test_top_rejects_a_non_positive_watch(monkeypatch):
    _install(monkeypatch, [A])

    result = CliRunner().invoke(cli, ["top", "train", "--watch", "0"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR


def test_top_watch_samples_until_interrupted(monkeypatch):
    calls = _install(monkeypatch, [A])
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(top_module.time, "sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["top", "train", "--watch", "2.5", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert sleeps == [2.5, 2.5, 2.5]
    assert len(calls) == 3  # initial sample + two refreshes before the interrupt
    assert all(json.loads(line)["ok"] for line in result.output.strip().splitlines())


def test_watch_in_table_mode_clears_between_samples(monkeypatch):
    _install(monkeypatch, [A])
    cleared = []
    monkeypatch.setattr(top_module.click, "clear", lambda: cleared.append(True))

    def fake_sleep(seconds):
        if cleared:
            raise KeyboardInterrupt

    monkeypatch.setattr(top_module.time, "sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["top", "train", "--watch", "1"])

    assert result.exit_code == 0, result.output
    assert cleared == [True]


# --- display helpers ---------------------------------------------------------------------

def test_summary_line_handles_missing_numbers():
    unknown = GpuStats(index=0, name="X", utilization_pct=None, memory_used_mib=None,
                       memory_total_mib=None, temperature_c=None, power_draw_w=None)

    assert display.summary_line([display.pod_reading(A, gpus=[unknown])]) == "1 GPU on 1 pod, util unknown"
    assert display.summary_line([display.pod_reading(A, error="boom")]) == "no GPU readings from 1 pod"


@pytest.mark.parametrize("gpu, expected", [
    (GPUS[0], "60.0/79.6 GiB (75%)"),
    (GpuStats(index=0, name="X", utilization_pct=None, memory_used_mib=None, memory_total_mib=None,
              temperature_c=None, power_draw_w=None), "-"),
])
def test_memory_cell(gpu, expected):
    assert display._memory(gpu) == expected
