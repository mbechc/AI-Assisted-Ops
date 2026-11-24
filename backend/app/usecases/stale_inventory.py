"""Stale inventory: devices unreachable for months that nobody marked as expected-down.

Replaced, stolen or decommissioned devices stay in the Catalyst Center inventory, count against licences,
skew health scores and show up in every report. The task proposes deleting devices that have been
unreachable for a long time. Devices that are excluded on purpose (maintenance mode or the exclusion tag of
Expected-down and flapping devices, or its expected-down sites and hostname prefixes) are kept.

Deleting is never automatic: an operator approves every item.
"""
from collections import Counter
from datetime import datetime, timezone

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, is_unreachable, now as utcnow
from app.models import DeviceStateHistory, InventoryDevice, TaskConfig, WorkItem
from app.usecases.common import aware, open_targets, split_list
from app.usecases.expected_down import EXCLUSION_TAG, list_excluded

EXPECTED_DOWN_TASK = "expected_down_devices"


@register
class StaleInventory(TaskDefinition):
    key = "stale_inventory"
    name = "Stale inventory"
    description = ("Finds devices that have been unreachable for months and are not excluded on purpose, and "
                   "proposes deleting them from the Catalyst Center inventory. Devices in maintenance mode, with "
                   "the exclusion tag or on expected-down sites are kept. Every deletion needs approval.")
    category = "Health / Inventory"
    worker = "health"
    default_cron = "40 6 * * *"
    max_autonomy = 1
    default_autonomy_level = 1
    default_autonomy_cap = 1
    action_types = {"delete_device": "Delete device from the Catalyst Center inventory"}
    params = [
        ParamSpec("min_days", "Unreachable for at least (days)", "int", 90),
        ParamSpec("include_aps", "Include access points", "bool", True),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 600),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        now = datetime.now(timezone.utc)
        ed = ctx.db.get(TaskConfig, EXPECTED_DOWN_TASK)
        ed_params = (ed.params if ed else None) or {}
        site_paths = [x.lower() for x in split_list(ed_params.get("expected_down_site_paths"))]
        prefixes = [x.lower() for x in split_list(ed_params.get("expected_down_hostname_prefixes"))]
        try:
            excluded = list_excluded(ctx.cc, ed_params.get("exclusion_tag") or EXCLUSION_TAG)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"Could not read current exclusions; nothing proposed: {exc}")
            return AnalysisResult(proposals=[], stats={"exclusions_unavailable": 1})

        site_label = {s.id: s.hierarchy for s in inv.sites}
        min_days = int(p.get("min_days", 90))
        proposals: list[Proposal] = []
        stats = Counter()
        for d in inv.devices:
            if not is_unreachable(d.reachability) or (d.is_ap and not p.get("include_aps", True)):
                continue
            row = inv.rows.get(d.id)
            since = [x for x in (aware(row.unreachable_since) if row else None,
                                 aware(row.last_update_at) if row else None) if x]
            days = (now - min(since)).total_seconds() / 86400 if since else 0
            if days < min_days:
                continue
            site = site_label.get(inv.assignments.get(d.id), "")
            if d.id in excluded:
                stats["kept_excluded"] += 1
                continue
            if (site_paths and any(site.lower().startswith(sp) for sp in site_paths)) or \
                    (prefixes and any((d.hostname or "").lower().startswith(px) for px in prefixes)):
                stats["kept_expected_down"] += 1
                continue
            stats["stale"] += 1
            proposals.append(self._proposal(d, days, site, row))

        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"stale": False} for t in open_targets(ctx, self.key) if t not in keys}
        ctx.log(f"{stats['stale']} devices unreachable for {min_days} days or more; kept {stats['kept_excluded']} "
                f"excluded and {stats['kept_expected_down']} on expected-down sites or prefixes")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "stale_devices": len(proposals), "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, d, days: float, site: str, row) -> Proposal:
        text = f"Unreachable for {days:.0f} days" + (f" ({row.reachability_failure})"
                                                     if row and row.reachability_failure else "")
        return Proposal(
            target_key=d.id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})",
            action_type="delete_device",
            payload={"device_id": d.id, "hostname": d.hostname, "site": site, "days": round(days),
                     "family": d.family, "platform": d.platform, "serial": d.serial},
            rule_confidence=90 if days >= 365 else 80,
            evidence=[{"signal": "reachability", "label": "Reachability", "available": True, "weight": 1.0,
                       "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=(f"{text}. Delete it from the Catalyst Center inventory, or exclude it with Expected-down "
                       "and flapping devices if it is coming back."),
            concerns=["Check that the device is decommissioned. Remove it from fabric and port assignments first "
                      "if it is part of SD-Access."],
            group_label=site.split("/")[-2] if site.count("/") >= 2 else (site or "No site"),
            fingerprint=f"stale:{d.id}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        timeout = int(ctx.params.get("task_timeout_seconds", 600))
        results: dict[int, ExecResult] = {}
        for it in items:
            dev = it.payload["device_id"]
            try:
                res = cc.wait_for(cc.delete_device(dev), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results[it.id] = ExecResult(False, f"delete failed: {exc}")
                continue
            if not res.success:
                results[it.id] = ExecResult(False, f"Catalyst Center task failed: {res.detail}")
                continue
            try:
                gone = dev not in cc.get_devices_by_id([dev])
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"verification read failed: {exc}")
                gone = None
            if gone:
                self._mark_removed(ctx, dev)
            results[it.id] = ExecResult(gone is not False, "deleted from the inventory" if gone else
                                        "delete requested (verification unavailable)" if gone is None else
                                        "delete task succeeded but the device is still in the inventory",
                                        verified=gone)
        return results

    @staticmethod
    def _mark_removed(ctx: RunContext, dev: str) -> None:
        row = ctx.db.get(InventoryDevice, (ctx.source.id, dev))
        if row is not None and row.removed_at is None:
            ts = utcnow()
            row.removed_at = ts
            ctx.db.add(DeviceStateHistory(source_id=ctx.source.id, device_id=dev, ts=ts, field="presence",
                                          old="present", new="removed"))
            ctx.db.flush()
