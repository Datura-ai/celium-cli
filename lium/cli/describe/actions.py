import contextlib

from lium.sdk import Lium, PodInfo
from lium.cli.utils import CliFailure, EXIT_POD_NOT_FOUND, loading_status


def resolve_pod_or_fail(lium: Lium, target: str, show_progress: bool) -> PodInfo:
    """The pod matching TARGET by id, huid or name; no match is a hard failure.

    Errors raised by the SDK travel up untouched — classifying them here by
    message would misreport an API outage as a mistyped pod id.
    """
    with loading_status("Loading pod", "") if show_progress else contextlib.nullcontext():
        pods = lium.ps()

    pod = next((p for p in pods if target in (p.id, p.huid, p.name)), None)
    if pod is None:
        raise CliFailure("pod_not_found", f"Pod '{target}' not found", EXIT_POD_NOT_FOUND)

    return pod
