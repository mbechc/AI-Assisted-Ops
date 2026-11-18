"""Shared collector: one inventory pass per Catalyst Center source for all specialists."""
from sqlalchemy import select

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import collect
from app.models import DataSource
from app.sources import build_cc_client, save_capabilities


@register
class InventorySnapshotTask(TaskDefinition):
    key = "cc_inventory_snapshot"
    name = "Inventory snapshot (collector)"
    description = ("Collects devices, site assignments, the site hierarchy and topology from every enabled "
                   "Catalyst Center once, and records changes per device (hostname, reachability, collection "
                   "status, site, role). All specialist tasks work on this snapshot.")
    category = "Collector"
    worker = "default"
    default_cron = "15 */2 * * *"
    max_autonomy = 0
    default_autonomy_level = 0
    default_autonomy_cap = 0
    params = [
        ParamSpec("all_sources", "Collect from every enabled Catalyst Center", "bool", True,
                  "When off, only the task's selected source is collected."),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        stats = {}
        snap = collect(ctx.db, ctx.source.id, ctx.cc, ctx.log)
        stats[ctx.source.name] = {"devices": snap.device_count, "aps": snap.ap_count, **snap.stats}
        if ctx.params.get("all_sources", True):
            others = ctx.db.scalars(select(DataSource).where(
                DataSource.kind == "catalyst_center", DataSource.enabled.is_(True),
                DataSource.id != ctx.source.id)).all()
            for src in others:
                client = build_cc_client(src)
                try:
                    client.connect()
                    ctx.log(f"Collecting {src.name}")
                    s = collect(ctx.db, src.id, client, ctx.log)
                    stats[src.name] = {"devices": s.device_count, "aps": s.ap_count, **s.stats}
                    save_capabilities(ctx.db, src, client)
                except Exception as exc:  # noqa: BLE001 - one unreachable source must not block the others
                    ctx.log(f"{src.name}: collection failed: {exc}")
                    stats[src.name] = {"error": str(exc)}
                finally:
                    client.close()
        return AnalysisResult(proposals=[], stats={"sources": stats})

    def execute(self, ctx: RunContext, items) -> dict[int, ExecResult]:
        return {}
