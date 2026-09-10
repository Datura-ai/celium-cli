"""Remove (rm) command implementation."""

import shlex
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, NoReturn, Optional
import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.workspaces.context import show_workspace
from lium.cli.interactive import noninteractive_reason
from lium.cli.utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    TargetMatch,
    handle_errors,
)
from . import validation, parsing
from .actions import RemovePodsAction, ScheduleRemovalAction


@dataclass(frozen=True)
class RemovalPlan:
    """Which pods to remove, and when — now if no time was given."""

    pods: List[PodInfo]
    termination_time: Optional[datetime]
    # Targets that were `lium ps` row numbers rather than names; the command
    # spells out what each one resolved to before acting on it.
    index_matches: List[TargetMatch] = field(default_factory=list)


def build_removal_plan(
    lium: Lium,
    targets: Optional[str],
    remove_all: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
    allow_index: Optional[bool] = None,
) -> Optional[RemovalPlan]:
    """Resolve TARGETS into a plan. None means there was nothing to remove."""
    is_valid, error = validation.validate(targets, remove_all, in_duration, at_time)
    if not is_valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    all_pods = ui.load("Loading pods", lambda: lium.ps())
    if not all_pods:
        # Removing everything from an empty account is a no-op, not a failure.
        # Naming a pod that is not there is a failure — that is a typo, and it
        # must not read like a successful teardown.
        if remove_all:
            ui.warning("No active pods")
            return None
        raise CliFailure(
            "pod_not_found", f"{parsing.NO_MATCHING_PODS}: {targets}", EXIT_POD_NOT_FOUND
        )

    parsed, error = parsing.parse(
        targets, remove_all, all_pods, in_duration, at_time, allow_index=allow_index
    )
    if error:
        raise CliFailure(
            "pod_not_found" if error.startswith(parsing.NO_MATCHING_PODS) else "invalid_arguments",
            error,
            EXIT_POD_NOT_FOUND
            if error.startswith(parsing.NO_MATCHING_PODS)
            else EXIT_CONFIGURATION_ERROR,
        )

    return RemovalPlan(
        pods=parsed["selected_pods"],
        termination_time=parsed.get("termination_time"),
        index_matches=parsed.get("index_matches", []),
    )


def describe_index_match(match: TargetMatch) -> str:
    """'1 → eager-wolf-aa (name: train)': what a row number stands for."""
    pod = match.pod
    name = f" (name: {pod.name})" if pod.name and pod.name != pod.huid else ""
    return f"{match.target} → {pod.huid}{name}"


def human_approved_index_targets(matches: List[TargetMatch], yes: bool = False) -> bool:
    """Show what each row number resolved to; ask before acting when someone can answer.

    A number is the one way to name a pod the caller may never have looked at, so
    the pod behind it is spelled out here — huid and name — before anything is
    removed, with or without --yes. Reached only on a terminal or with ``--yes``:
    :func:`refuse_without_a_terminal` has already turned away a piped caller
    that gave no flag.
    """
    for match in matches:
        ui.info(f"Pod {describe_index_match(match)}")
    if yes:
        return True
    try:
        return ui.confirm(f"Remove {len(matches)} pod(s) selected by index?", hint="pass --yes to remove without a prompt")
    except EOFError:
        ui.warning("\nNo answer — nothing removed")
        return False


def human_approved_removing_every_pod(pods: List[PodInfo]) -> bool:
    """Ask on a terminal before wiping the whole account.

    Reached only when a human can answer: the command skips it with ``--yes``,
    and :func:`refuse_without_a_terminal` has already turned away a piped caller.
    """
    listed_huids = ", ".join(pod.huid for pod in pods)
    try:
        return ui.confirm(f"Remove all {len(pods)} pods ({listed_huids})?", hint="pass --yes to remove every pod without a prompt")
    except EOFError:
        # The terminal went away mid-prompt. No answer is not a yes.
        ui.warning("\nNo answer — nothing removed")
        return False


def rerun_with_yes(
    targets: Optional[str],
    remove_all: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
    name_only: bool,
) -> str:
    """The command line that was given, with ``--yes`` added — what a refused caller re-runs."""
    words = ["lium", "rm"]
    if targets:
        words.append(shlex.quote(targets))
    if remove_all:
        words.append("--all")
    if in_duration:
        words.extend(["--in", shlex.quote(in_duration)])
    if at_time:
        words.extend(["--at", shlex.quote(at_time)])
    if name_only:
        words.append("--name-only")
    words.append("--yes")
    return " ".join(words)


def refuse_without_a_terminal(plan: RemovalPlan, rerun: str) -> NoReturn:
    """Without a terminal, ``--yes`` is the only approval ``lium rm`` accepts.

    A pipe on stdin is not a yes: a script, an agent or an ``echo y |`` that
    named a pod has said which pod, not that it may go (DAH-2556 read a piped
    caller as approval; the loop lost a pod that way). So a caller nobody can
    prompt fails with ``confirmation_required`` (exit 2) — one message naming
    every pod the command would have acted on and the same command line with
    ``--yes`` — and nothing is removed or scheduled.
    """
    verb = "schedule removal of" if plan.termination_time else "remove"
    listed_huids = ", ".join(pod.huid for pod in plan.pods)
    hint = f"Re-run with --yes: {rerun}"
    raise CliFailure(
        "confirmation_required",
        f"Would {verb} {len(plan.pods)} pod(s): {listed_huids} — nothing done because "
        f"{noninteractive_reason()}. {hint}",
        EXIT_CONFIGURATION_ERROR,
        hint=hint,
    )


@click.command("rm")
@click.argument("targets", required=False)
@click.option("--all", "-a", "remove_all", is_flag=True, help="Remove all active pods")
@click.option(
    "--yes", "-y", is_flag=True,
    help="Skip the confirmation prompt; required when stdin is not a terminal (scripts, pipes).",
)
@click.option("--in", "in_duration", help="Schedule removal after duration")
@click.option("--at", "at_time", help="Schedule removal at time")
@click.option(
    "--name-only",
    "name_only",
    is_flag=True,
    help="Treat TARGETS as ids, names or huids only; never as 'lium ps' row numbers (for scripts).",
)
@handle_errors
def rm_command(
    targets: Optional[str],
    remove_all: bool,
    yes: bool,
    in_duration: Optional[str],
    at_time: Optional[str],
    name_only: bool,
):
    """Remove (terminate) GPU pods.

    \b
    TARGETS: comma-separated pod huids, names or ids (eager-wolf-aa,my-pod).
    A row number from your last 'lium ps' in this shell (1, 2) stands for the
    pod that listing showed there; it is accepted only while that pod is still
    listed and for 10 minutes after the listing. The pod list is account-wide
    and changes as pods come and go. Use --name-only or LIUM_NO_POD_INDEX=1 to
    refuse numbers altogether.

    \b
    Removal is irreversible. Exits non-zero when nothing matched TARGETS, so a
    typo cannot look like a successful teardown. Without a terminal on stdin,
    --yes is required: the command names the pods it would remove and exits 2.
    """
    lium = Lium()
    show_workspace(lium, acting=True)
    plan = build_removal_plan(
        lium, targets, remove_all, in_duration, at_time, allow_index=False if name_only else None
    )
    if plan is None:
        return

    if not yes and not ui.is_interactive():
        refuse_without_a_terminal(
            plan, rerun_with_yes(targets, remove_all, in_duration, at_time, name_only)
        )

    if remove_all and not yes and not human_approved_removing_every_pod(plan.pods):
        return

    if plan.index_matches and not human_approved_index_targets(plan.index_matches, yes):
        return

    context = {"pods": plan.pods, "lium": lium}
    if plan.termination_time:
        context["termination_time"] = plan.termination_time.isoformat()
        action = ScheduleRemovalAction()
        done_verb = "Scheduled removal for"
    else:
        action = RemovePodsAction()
        done_verb = "Removed"

    failed_huids = action.execute(context).data["failed_huids"]
    removed_huids = [pod.huid for pod in plan.pods if pod.huid not in failed_huids]

    # Say what happened: silence is indistinguishable from having done nothing.
    if removed_huids:
        ui.success(f"{done_verb} {len(removed_huids)} pod(s): {', '.join(removed_huids)}")

    if failed_huids:
        raise CliFailure(
            "removal_failed",
            f"Failed to remove pods: {', '.join(failed_huids)}",
            EXIT_GENERAL_ERROR,
        )
