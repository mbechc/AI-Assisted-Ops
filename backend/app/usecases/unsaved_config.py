"""Unsaved configuration: devices whose running configuration has not been saved to startup for days.

Someone changed a device and never ran "write memory". The change is lost on the next reload, and Catalyst
Center keeps flagging the device as non-compliant on "Startup vs Running". The task reads that compliance
result from Catalyst Center and, on releases with the configuration API (2.3.7 and later), proposes saving the
running configuration through Catalyst Center. On older releases the findings are an owner report.
"""
from collections import Counter
from datetime import datetime, timezone

from app.connectors.catalyst_center.client import CapabilityUnavailable
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, is_unreachable
from app.models import WorkItem
from app.store import cache_get, cache_set
from app.usecases.common import chunks, fail_all, open_targets

FIRST_SEEN_TTL = 400 * 86_400


def bucket(days: float) -> str:
    if days < 7:
        return "Unsaved for under a week"
    if days < 28:
        return "Unsaved for 1-4 weeks"
    return "Unsaved for over 4 weeks"


@register
class UnsavedConfiguration(TaskDefinition):
    key = "unsaved_configuration"
    name = "Unsaved configuration"
    description = ("Finds devices whose running configuration has differed from the startup configuration for "
                   "days (Catalyst Center compliance: Startup vs Running). Proposes saving it through the "
                   "Catalyst Center API (2.3.7 and later); on older releases it reports the devices.")
    category = "Health / Configuration"
    worker = "health"
    default_cron = "10 */6 * * *"
    max_autonomy = 2
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 25
    action_types = {"write_memory": "Save running configuration to startup via Catalyst Center",
                    "report": "Owner report (no change)"}
    params = [
        ParamSpec("min_days", "Unsaved for at least (days)", "float", 3),
        ParamSpec("batch_size", "Devices per save call", "int", 25),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 600),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        now = datetime.now(timezone.utc)
        try:
            rows = ctx.cc.get_config_compliance()
        except CapabilityUnavailable as exc:
            ctx.log(f"Compliance API not available on this Catalyst Center: {exc}")
            return AnalysisResult(proposals=[], stats={"api_unavailable": 1})
        can_write = ctx.cc.supports("write_memory")
        if not can_write:
            ctx.log("This Catalyst Center release has no write-memory API (2.3.7 and later); findings are reported")

        key = f"unsaved_first_seen:{ctx.source.id}"
        seen = dict(cache_get(ctx.db, key) or {})
        unsaved = {r.device_id: r for r in rows if r.status == "NON_COMPLIANT"}
        seen = {d: seen.get(d) or now.isoformat() for d in unsaved}
        cache_set(ctx.db, key, seen, FIRST_SEEN_TTL)

        site_label = {s.id: s.hierarchy for s in inv.sites}
        min_days = float(p.get("min_days", 3))
        proposals: list[Proposal] = []
        stats = Counter(checked=len(rows), non_compliant=len(unsaved))
        by_id = {d.id: d for d in inv.devices}
        for dev_id, row in unsaved.items():
            d = by_id.get(dev_id)
            if d is None or d.is_ap:
                stats["not_in_inventory"] += 1
                continue
            if is_unreachable(d.reachability):
                stats["unreachable"] += 1
                continue
            since = [datetime.fromisoformat(seen[dev_id])]
            if row.last_change_ms:
                since.append(datetime.fromtimestamp(row.last_change_ms / 1000, timezone.utc))
            days = (now - min(since)).total_seconds() / 86400
            if days < min_days:
                stats["recent"] += 1
                continue
            stats["unsaved"] += 1
            site = site_label.get(inv.assignments.get(dev_id), "")
            proposals.append(self._proposal(d, days, site, can_write))

        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"saved": True} for t in open_targets(ctx, self.key) if t not in unsaved and t not in keys}
        ctx.log(f"{stats['non_compliant']} of {stats['checked']} devices differ from startup; "
                f"{stats['unsaved']} for {min_days:g} days or more")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "unsaved_devices": len(proposals), "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, d, days: float, site: str, can_write: bool) -> Proposal:
        text = f"Running configuration differs from startup for {days:.0f} days"
        return Proposal(
            target_key=d.id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})",
            action_type="write_memory" if can_write else "report",
            payload={"device_id": d.id, "hostname": d.hostname, "site": site, "days": round(days, 1),
                     "family": d.family, "platform": d.platform},
            rule_confidence=90 if days >= 7 else 85,
            evidence=[{"signal": "compliance", "label": "Startup vs Running", "available": True, "weight": 1.0,
                       "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=(f"{text}. Save it so the change survives a reload." if can_write else
                       f"{text}. Save it on the device (write memory) or review the change with its owner."),
            concerns=["Saving makes every change since the last save permanent, including experiments."],
            blocked_reason=None if can_write else
            "Owner action needed: this Catalyst Center release cannot save the configuration (2.3.7 and later).",
            group_label=bucket(days),
            fingerprint=f"unsaved:{d.id}:{bucket(days)}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        timeout = int(ctx.params.get("task_timeout_seconds", 600))
        results: dict[int, ExecResult] = {}
        results.update(fail_all([i for i in items if i.action_type != "write_memory"],
                                "owner report: nothing to execute"))
        todo = [i for i in items if i.action_type == "write_memory"]
        for batch in chunks(todo, int(ctx.params.get("batch_size", 25))):
            ids = [i.payload["device_id"] for i in batch]
            ctx.log(f"Saving the running configuration on {len(ids)} device(s)")
            try:
                res = cc.wait_for(cc.write_memory(ids), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all(batch, f"save failed: {exc}"))
                continue
            if not res.success:
                results.update(fail_all(batch, f"Catalyst Center task failed: {res.detail}"))
                continue
            try:
                cc.wait_for(cc.run_compliance(ids, ["RUNNING_CONFIG"]), timeout=min(timeout, 300))
                now = {r.device_id: r.status for r in cc.get_config_compliance(ids)}
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"compliance re-check failed: {exc}")
                now = None
            for it in batch:
                status = (now or {}).get(it.payload["device_id"])
                verified = True if status == "COMPLIANT" else None
                results[it.id] = ExecResult(True, "saved; Startup vs Running is compliant again" if verified else
                                            "saved (compliance not re-checked yet)", verified=verified)
        return results
