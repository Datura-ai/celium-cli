"""`lium spend`: what the active pods cost and how long the balance lasts."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors
from . import report as report_module


def _gather(lium: Lium):
    pods = lium.ps()
    try:
        balance = lium.balance()
    except Exception as exc:  # noqa: BLE001 - the per-pod figures are still worth showing
        ui.notice_debug(f"balance unavailable: {exc}")
        balance = None
    return report_module.build_report(pods, balance)


@click.command("spend")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def spend_command(json_output: bool):
    """Show the hourly burn, the estimated spend per active pod, and the runway left.

    Spent is price × wall time since each pod was created; the API does not
    report billed amounts, so treat it as an estimate.

    \b
    Examples:
      lium spend
      lium spend --json | jq '.burn_per_hour, .runway_hours'
    """
    lium = Lium()
    report = _gather(lium) if json_output else ui.load("Loading pods and balance", lambda: _gather(lium))

    if json_output:
        click.echo(json.dumps(report.to_dict(), sort_keys=True))
        return

    if not report.pods:
        ui.info("No active pods; burn is $0.00/h")
        if report.balance_usd is not None:
            ui.dim(f"Balance ${report.balance_usd:,.2f}")
        return

    ui.print(report_module.build_table(report))
    for line in report_module.summary_lines(report):
        ui.dim(line)
