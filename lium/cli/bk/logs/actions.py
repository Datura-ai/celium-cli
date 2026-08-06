from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class GetBackupLogsAction:
    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo | None = ctx.get("pod")
        backup_id: str | None = ctx.get("backup_id")

        if backup_id:
            # Search for specific backup ID across all pods
            all_pods = lium.ps()
            for pod_info in all_pods:
                pod_name_search = pod_info.name or pod_info.huid
                logs = lium.backup_logs(pod=pod_info)
                for log in logs:
                    if getattr(log, 'id', '').startswith(backup_id):
                        return ActionResult(
                            ok=True,
                            data={
                                "single_backup": True,
                                "pod_name": pod_name_search,
                                "log": log
                            }
                        )
            return ActionResult(ok=False, error=f"Backup '{backup_id}' not found", data={})

        # Get logs for specific pod
        backup_logs = lium.backup_logs(pod=pod) if pod else []

        if not backup_logs:
            return ActionResult(ok=True, data={"logs": []})

        return ActionResult(ok=True, data={"logs": backup_logs[:10]})
