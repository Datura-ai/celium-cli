from lium.cli.actions import ActionResult


class GetExecutorsAction:
    """Get available executors."""

    def execute(self, ctx: dict) -> ActionResult:
        """Get executors list.

        """
        lium = ctx["lium"]

        try:
            executors = lium.ls(
                gpu_type=ctx.get("gpu_type"),
                gpu_count=ctx.get("gpu_count"),
                lat=ctx.get("lat"),
                lon=ctx.get("lon"),
                max_distance_miles=ctx.get("max_distance"),
                min_cuda_version=ctx.get("min_cuda_version"),
                widen_for_splitting=True,
                tier=ctx.get("tier"),
                min_reliability=ctx.get("min_reliability"),
                include_unscored=ctx.get("include_unscored", True),
                min_uptime_minutes=ctx.get("min_uptime_minutes"),
                min_vram_gb=ctx.get("min_vram_gb"),
                min_vram_total_gb=ctx.get("min_vram_total_gb"),
                min_ports=ctx.get("min_ports"),
                countries=ctx.get("countries"),
                max_price_total=ctx.get("max_price_total"),
                max_price_per_gpu=ctx.get("max_price_per_gpu"),
            )
            return ActionResult(
                ok=True,
                data={"executors": executors}
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
