from typing import List

from lium.cli.actions import ActionResult


class GetPodsAction:
    """Get active pods."""

    def execute(self, ctx: dict) -> ActionResult:
        """Get pods list.

        Context:
            lium: Lium SDK instance
        """
        lium = ctx["lium"]

        pods = lium.ps()
        return ActionResult(
            ok=True,
            data={"pods": pods}
        )
