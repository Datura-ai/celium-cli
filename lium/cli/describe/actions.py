from lium.cli.actions import ActionResult


class GetPodAction:
    """Resolve one pod by id, huid or name."""

    def execute(self, ctx: dict) -> ActionResult:
        """Find the pod a caller named.

        Context:
            lium: Lium SDK instance
            target: pod id, huid or name
        """
        lium = ctx["lium"]
        target = ctx["target"]

        try:
            pods = lium.ps()
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))

        pod = next((p for p in pods if target in (p.id, p.huid, p.name)), None)
        if pod is None:
            return ActionResult(ok=False, data={}, error=f"Pod '{target}' not found")

        return ActionResult(ok=True, data={"pod": pod})
