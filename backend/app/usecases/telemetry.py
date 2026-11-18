"""Telemetry baseline: devices that Catalyst Center no longer collects data from.

* Stale sync (managed and reachable, but no inventory update for days): propose a resync via Catalyst
  Center. This is safe and can run automatically up to L3.
* Credential and SNMP failures: a resync does not help; they go to an owner report grouped by cause, with
  the Catalyst Center error so the device owner can fix the credentials or SNMP settings.
Unreachable devices are handled by the expected-down task.
"""
import time
from collections import Counter
from datetime import datetime, timezone

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, is_unreachable
from app.models import WorkItem
from app.usecases.common import chunks, fail_all, open_targets

CREDENTIAL_WORDS = ("auth", "credential", "password", "login", "netconf")
SNMP_WORDS = ("snmp",)
LABELS = {"credential": "Credential failure", "snmp": "SNMP failure", "sync_stale": "Stale sync",
          "collection_failed": "Collection failure"}
ADVICE = {
    "credential": "Fix the CLI/NETCONF credentials for this device in Catalyst Center (or on the device), then resync.",
    "snmp": "Check the SNMP version and credentials configured in Catalyst Center against the device, then resync.",
    "collection_failed": "Check the Catalyst Center collection error for this device.",
}


def classify(row, now: datetime, stale_hours: float) -> tuple[str | None, str]:
    status = (row.collection_status or "").lower()
    text = " ".join(x for x in (row.error_code, row.error_description, row.collection_status) if x).lower()
    managed = status in ("", "managed", "in progress")
    if not managed or row.error_code:
        if any(w in text for w in SNMP_WORDS):
            return "snmp", row.error_description or row.collection_status or ""
        if any(w in text for w in CREDENTIAL_WORDS):
            return "credential", row.error_description or row.collection_status or ""
        return "collection_failed", row.error_description or row.collection_status or ""
    if row.last_update_at is not None:
        last = row.last_update_at if row.last_update_at.tzinfo else row.last_update_at.replace(tzinfo=timezone.utc)
        hours = (now - last).total_seconds() / 3600
        if hours >= stale_hours:
            return "sync_stale", f"last inventory update {hours / 24:.1f} days ago"
    return None, ""


@register
class TelemetryBaseline(TaskDefinition):
    key = "telemetry_baseline"
    name = "Telemetry baseline"
    description = ("Finds managed devices Catalyst Center no longer collects from. Stale syncs get a resync via "
                   "the Catalyst Center API; credential and SNMP failures go to an owner report with the error, "
                   "because a resync cannot fix them.")
    category = "Health / Telemetry"
    worker = "health"
    default_cron = "40 */6 * * *"
    max_autonomy = 3
    default_autonomy_level = 1
    default_autonomy_cap = 2
    default_auto_threshold = 85
    default_max_auto_per_run = 50
    action_types = {"resync_device": "Resync device inventory in Catalyst Center",
                    "report": "Owner report (no change)"}
    params = [
        ParamSpec("stale_hours", "No inventory update for (hours) counts as stale", "float", 48),
        ParamSpec("include_aps", "Include access points", "bool", False),
        ParamSpec("resync_batch_size", "Devices per resync call", "int", 25),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 600),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        now = datetime.now(timezone.utc)
        site_label = {s.id: s.hierarchy for s in inv.sites}
        stale_hours = float(p.get("stale_hours", 48))
        proposals: list[Proposal] = []
        stats = Counter()
        for d in inv.devices:
            if d.is_ap and not p.get("include_aps", False):
                continue
            row = inv.rows.get(d.id)
            if row is None or is_unreachable(d.reachability):
                continue
            stats["checked"] += 1
            cls, detail = classify(row, now, stale_hours)
            if cls is None:
                continue
            stats[cls] += 1
            site = site_label.get(inv.assignments.get(d.id), "")
            proposals.append(self._proposal(d, row, cls, detail, site))

        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"telemetry_ok": True} for t in open_targets(ctx, self.key) if t not in keys}
        ctx.log(f"Checked {stats['checked']} reachable devices: {stats['sync_stale']} stale, "
                f"{stats['credential']} credential, {stats['snmp']} SNMP, {stats['collection_failed']} other failures")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "without_telemetry": len(proposals), "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, d, row, cls: str, detail: str, site: str) -> Proposal:
        resync = cls == "sync_stale"
        text = f"{LABELS[cls]}: {detail}" + (f" (error code {row.error_code})" if row.error_code else "")
        return Proposal(
            target_key=d.id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})",
            action_type="resync_device" if resync else "report",
            payload={"device_id": d.id, "hostname": d.hostname, "class": cls, "site": site, "platform": d.platform,
                     "collection_status": row.collection_status, "error_code": row.error_code,
                     "error_description": row.error_description, "auto_eligible": resync,
                     "advice": None if resync else ADVICE[cls]},
            rule_confidence=90 if resync else 95,
            evidence=[{"signal": cls, "label": LABELS[cls], "available": True, "weight": 1.0, "detail": text,
                       "buildings": [], "floors": [], "agrees": True}],
            rationale=("Resync the device so Catalyst Center collects fresh inventory. " if resync
                       else ADVICE[cls] + " ") + text,
            blocked_reason=None if resync else "Owner action needed: a resync cannot fix this.",
            group_label=LABELS[cls],
            fingerprint=f"{cls}:{d.id}:{row.error_code or ''}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        timeout = int(ctx.params.get("task_timeout_seconds", 600))
        results: dict[int, ExecResult] = {}
        report = [i for i in items if i.action_type != "resync_device"]
        results.update(fail_all(report, "owner report: nothing to execute"))
        todo = [i for i in items if i.action_type == "resync_device"]
        for batch in chunks(todo, int(ctx.params.get("resync_batch_size", 25))):
            ids = [i.payload["device_id"] for i in batch]
            started_ms = int(time.time() * 1000) - 60_000
            ctx.log(f"Resyncing {len(ids)} device(s)")
            try:
                res = cc.wait_for(cc.sync_devices(ids), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all(batch, f"resync failed: {exc}"))
                continue
            if not res.success:
                results.update(fail_all(batch, f"Catalyst Center task failed: {res.detail}"))
                continue
            try:
                now = cc.get_devices_by_id(ids)
            except Exception as exc:  # noqa: BLE001
                now = None
                ctx.log(f"verification read failed: {exc}")
            for it in batch:
                dev = now.get(it.payload["device_id"]) if now else None
                verified = None if now is None else bool(dev and (dev.last_update_ms or 0) >= started_ms)
                results[it.id] = ExecResult(
                    success=verified is not False,
                    detail=("resynced; fresh inventory confirmed" if verified else
                            "resync requested (verification unavailable)" if verified is None else
                            "resync task succeeded but no fresh inventory yet"),
                    verified=verified)
        return results
