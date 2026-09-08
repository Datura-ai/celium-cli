"""Audit command: who did what to the account's pods, and when."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click

from lium.sdk import Lium
from lium.sdk.exceptions import LiumAuthError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    ensure_config,
    handle_errors,
    parse_targets,
)
from lium.cli.rm.parsing import parse_duration

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# What a renter reads for the backend's sub_event_type; anything unlisted is shown as recorded.
_WHAT = {
    "pod-rent.requested": "rent requested",
    "pod-create.success": "created",
    "pod-create.failed": "create failed",
    "pod-delete.success": "delete requested",
    "pod-delete.failed": "delete failed",
    "worker-pod-delete.success": "deleted by the platform",
    "pod-reboot.success": "reboot requested",
    "pod-reboot.failed": "reboot failed",
    "pod-edit.success": "edited",
    "pod-switch-template.success": "template switched",
    "pod-add-ssh-key.success": "ssh key added",
    "pod-host-reboot-recovery.recovered": "recovered after host reboot",
    "api-key-create.success": "API key created",
    "api-key-delete.success": "API key deleted",
    "api-key-update.success": "API key renamed",
    "ssh-key-create.success": "SSH key added",
    "ssh-key-delete.success": "SSH key removed",
    "template-create.success": "template created",
    "template-delete.success": "template deleted",
}


def parse_since(value: str) -> datetime:
    """``24h`` / ``30m`` / ``7d`` counted back from now, or an ISO-8601 timestamp; UTC either way."""
    delta, _ = parse_duration(value)
    if delta is not None:
        return datetime.now(timezone.utc) - delta
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CliFailure(
            "invalid_since",
            f"Invalid --since '{value}'. Use a duration like 24h, 30m, 7d or an ISO timestamp.",
            EXIT_CONFIGURATION_ERROR,
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def what(event: Dict[str, Any]) -> str:
    """One phrase for the event: lifecycle entries say the status change and its reason."""
    sub = event.get("sub_event_type") or event.get("event_type") or "?"
    if sub == "pod-lifecycle.status":
        text = f"→ {event.get('to_status')}"
        if event.get("reason"):
            text += f" ({event['reason']})"
    else:
        text = _WHAT.get(sub, sub)
    for extra in (event.get("detail"), event.get("error")):
        if extra:
            text += f": {extra}"
    return text


def who(event: Dict[str, Any]) -> str:
    actor = event.get("actor")
    if not actor:
        return "platform"
    if actor.get("auth") == "api_key":
        return f"key {actor.get('api_key_name') or '?'} ({(actor.get('api_key_id') or '')[:8]})"
    return actor.get("auth") or "?"


def _when(created_at: Optional[str]) -> str:
    if not created_at:
        return "—"
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)   # the column is UTC whatever offset the stamp carries
    return stamp.strftime("%Y-%m-%d %H:%M:%SZ")


def rows(events: List[Dict[str, Any]]) -> List[List[str]]:
    # oldest first: the API returns the tail newest-first, a log reads top-down
    return [
        [_when(e.get("created_at")), e.get("pod_name") or (e.get("pod_id") or "")[:8] or "—", what(e), who(e)]
        for e in reversed(events)
    ]


def _resolve_pod_id(lium: Lium, target: str) -> str:
    # a listed pod by id/huid/name/index; a bare UUID is passed through so a deleted pod can be asked about
    matches = parse_targets(target, lium.ps())
    if len(matches) == 1:
        return matches[0].id
    if _UUID.match(target):
        return target
    raise CliFailure(
        "pod_not_found", f"No listed pod matches '{target}'. For a deleted pod give its full id.", EXIT_POD_NOT_FOUND
    )


@click.command("audit")
@click.option("--pod", "pod", help="Only this pod: id, huid, name or index from the last ps; a deleted pod's full id")
@click.option("--since", "since", help="Only events after this: 24h, 30m, 7d or an ISO timestamp")
@click.option("--key", "api_key_id", help="Only actions made with this API key id")
@click.option("--limit", type=click.IntRange(1, 1000), default=200, show_default=True, help="Newest events to fetch (1–1000)")
@click.option("--json", "json_output", is_flag=True, help="Print the events as machine-readable JSON")
@handle_errors
def audit_command(pod: Optional[str], since: Optional[str], api_key_id: Optional[str], limit: int, json_output: bool):
    """Show who did what to the account's pods, and when.

    Every rent, reboot, edit and delete names the session or API key that requested it; entries the
    platform wrote by itself (a validator reply, a balance stop) say "platform".

    \b
    Examples:
      lium audit                       # last 200 events, oldest first
      lium audit --since 24h           # what happened today
      lium audit --pod my-pod          # one pod's history, also after it was deleted (full id)
      lium audit --key 3f2a...         # everything one API key did
      lium audit --json | jq '.[] | select(.actor.api_key_name == "ci")'
    """
    if not json_output:
        ensure_config()

    lium = Lium()
    since_at = parse_since(since) if since else None
    pod_id = _resolve_pod_id(lium, pod) if pod else None
    try:
        events = lium.events(since=since_at, pod_id=pod_id, api_key_id=api_key_id, limit=limit)
    except LiumAuthError as exc:
        # same exit code as every other command's 401 (handle_errors → EXIT_API_ERROR); only the hint is added
        raise CliFailure(
            "auth_error",
            f"{exc}. If the key works for 'lium ps', this backend does not yet open /users/me/events to API keys.",
            EXIT_API_ERROR,
        )

    if json_output:
        click.echo(json.dumps(events, indent=2, ensure_ascii=False))
        return

    if not events:
        ui.info("No events" + (f" since {since}" if since else "") + ".")
        return
    ui.table(["When (UTC)", "Pod", "What", "By"], rows(events))
