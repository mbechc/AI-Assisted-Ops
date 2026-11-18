"""Device role correction: infer ACCESS / DISTRIBUTION / CORE from the physical topology.

Layers are derived bottom-up from the CDP/LLDP graph in the inventory snapshot:
access switches have APs (or are leaves), distribution switches aggregate access switches, and core
switches interconnect distribution switches and wireless controllers. A switch whose Catalyst Center
role differs from its place in the graph (e.g. a 9300 acting as core but tagged ACCESS) gets a proposal
to set the role via the Catalyst Center API.
"""
from collections import Counter, defaultdict

from app.connectors.catalyst_center.models import Device
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, record_change
from app.models import WorkItem
from app.usecases.common import fail_all, open_targets

SWITCH = "switches and hubs"
WLC = "wireless controller"
MANAGED_ROLES = ("ACCESS", "DISTRIBUTION", "CORE")


def infer_roles(devices: list[Device], links) -> dict[str, tuple[str, int, list[str]]]:
    """device_id -> (role, confidence, reasons) for switches that have at least one link."""
    fam = {d.id: (d.family or "").lower() for d in devices}
    nb: dict[str, set[str]] = defaultdict(set)
    for ln in links:
        if ln.a_id in fam and ln.b_id in fam and ln.a_id != ln.b_id:
            nb[ln.a_id].add(ln.b_id)
            nb[ln.b_id].add(ln.a_id)
    switches = [d.id for d in devices if fam[d.id] == SWITCH and nb.get(d.id)]
    aps = {s: sum(1 for n in nb[s] if fam[n] == "unified ap") for s in switches}
    sw_nb = {s: {n for n in nb[s] if fam[n] == SWITCH} for s in switches}
    wlc_nb = {s: sum(1 for n in nb[s] if fam[n] == WLC) for s in switches}

    access = {s for s in switches if aps[s] > 0 or (len(sw_nb[s]) <= 1 and not wlc_nb[s])}
    dist = {s for s in switches if s not in access and sw_nb[s] & access}
    core = {s for s in switches if s not in access and s not in dist and (sw_nb[s] & dist or wlc_nb[s])}

    out: dict[str, tuple[str, int, list[str]]] = {}
    for s in switches:
        n_acc, n_dist, n_core = len(sw_nb[s] & access), len(sw_nb[s] & dist), len(sw_nb[s] & core)
        if s in access:
            reasons = [f"{aps[s]} AP(s) connected" if aps[s] else "leaf switch (single switch uplink)"]
            conf = 92 if aps[s] and len(sw_nb[s]) <= 2 else 80 if aps[s] else 70
            if wlc_nb[s] or n_core:
                conf -= 20
                reasons.append("also linked to core/WLC")
            out[s] = ("ACCESS", conf, reasons)
        elif s in dist:
            reasons = [f"aggregates {n_acc} access switch(es)"] + ([f"uplinked to {n_core} core switch(es)"] if n_core else [])
            conf = 90 if n_acc >= 2 and n_core else 80 if n_acc >= 2 else 65
            if wlc_nb[s]:
                conf -= 10
                reasons.append("a wireless controller is attached here")
            out[s] = ("DISTRIBUTION", conf, reasons)
        elif s in core:
            reasons = [f"interconnects {n_dist} distribution switch(es)"] if n_dist else []
            if wlc_nb[s]:
                reasons.append(f"{wlc_nb[s]} wireless controller(s) attached")
            if n_core:
                reasons.append(f"peers with {n_core} other core switch(es)")
            conf = 92 if n_dist >= 2 else 82 if n_dist >= 1 and (wlc_nb[s] or n_core) else 65
            out[s] = ("CORE", conf, reasons)
    return out


@register
class DeviceRoleCorrection(TaskDefinition):
    key = "device_role_correction"
    name = "Device role correction"
    description = ("Infers each switch's role (access, distribution, core) from the CDP/LLDP topology and "
                   "proposes correcting the role in Catalyst Center where it differs, e.g. 9300s acting as core "
                   "but tagged ACCESS. Roles drive topology views, assurance and design rules.")
    category = "Inventory hygiene"
    worker = "inventory"
    default_cron = "25 6 * * *"
    max_autonomy = 2
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 10
    action_types = {"set_role": "Set device role in Catalyst Center (inventory attribute only)"}
    params = [
        ParamSpec("min_confidence", "Minimum confidence to propose a role change", "int", 60),
        ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 300),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        if not inv.links:
            ctx.log("No topology links in the inventory snapshot: roles cannot be inferred")
        site_label = {s.id: s.hierarchy for s in inv.sites}
        inferred = infer_roles(inv.devices, inv.links)
        by_id = {d.id: d for d in inv.devices}
        min_conf = int(p.get("min_confidence", 60))
        proposals: list[Proposal] = []
        stats = Counter()
        for dev_id, (role, conf, reasons) in inferred.items():
            d = by_id[dev_id]
            cur = (d.role or "UNKNOWN").upper()
            stats[f"inferred_{role.lower()}"] += 1
            if cur == role:
                stats["consistent"] += 1
                continue
            if cur not in MANAGED_ROLES + ("UNKNOWN", ""):
                stats["skipped_other_role"] += 1
                continue
            if conf < min_conf:
                stats["low_confidence"] += 1
                continue
            stats["mismatch"] += 1
            site = site_label.get(inv.assignments.get(dev_id), "")
            text = f"Tagged {cur} in Catalyst Center but acts as {role}: " + "; ".join(reasons) + "."
            proposals.append(Proposal(
                target_key=dev_id, target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})", action_type="set_role",
                payload={"device_id": dev_id, "hostname": d.hostname, "platform": d.platform, "site": site,
                         "current_role": cur, "role": role, "reasons": reasons},
                rule_confidence=conf,
                evidence=[{"signal": "topology", "label": "Topology (CDP/LLDP)", "available": True, "weight": 1.0,
                           "detail": text, "buildings": [], "floors": [], "agrees": True}],
                rationale=f"Set role to {role}. " + text,
                concerns=[] if conf >= 85 else ["Topology evidence is limited; check the device's real function."],
                group_label=f"{cur} -> {role}",
                fingerprint=f"{dev_id}:{cur}->{role}",
            ))
        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"actual_role": (by_id[t].role if t in by_id else None)}
                    for t in open_targets(ctx, self.key) if t not in keys}
        ctx.log(f"Roles inferred for {len(inferred)} switches: {stats['consistent']} consistent, "
                f"{stats['mismatch']} mismatches proposed, {stats['low_confidence']} low confidence")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "switches_with_links": len(inferred), "cc_api_calls": ctx.cc.http.calls, **stats})

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "proposed": (item.payload or {}).get("role"),
                "matched": (observed.get("actual_role") or "").upper() == (item.payload or {}).get("role")}

    def apply_override(self, ctx: RunContext, item: WorkItem, override: dict) -> dict:
        role = (override.get("role") or "").upper()
        if role not in MANAGED_ROLES:
            raise ValueError("role must be ACCESS, DISTRIBUTION or CORE")
        return {"role": role}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        cc = ctx.cc
        timeout = int(ctx.params.get("task_timeout_seconds", 300))
        results: dict[int, ExecResult] = {}
        done: list[WorkItem] = []
        for it in items:
            pl = self.effective_payload(it)
            dev = Device(id=pl["device_id"], hostname=pl.get("hostname") or "", mgmt_ip=None, mac=None,
                         family="Switches and Hubs")
            try:
                res = cc.wait_for(cc.set_device_role(dev, pl["role"]), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                results.update(fail_all([it], f"set role failed: {exc}"))
                continue
            if not res.success:
                results.update(fail_all([it], f"Catalyst Center task failed: {res.detail}"))
                continue
            done.append(it)
        if done:
            try:
                now = cc.get_devices_by_id([i.payload["device_id"] for i in done])
            except Exception as exc:  # noqa: BLE001
                now = None
                ctx.log(f"verification read failed: {exc}")
            for it in done:
                want = self.effective_payload(it)["role"]
                actual = (now.get(it.payload["device_id"]).role if now and it.payload["device_id"] in now else None)
                verified = None if now is None else (actual or "").upper() == want
                if verified:
                    record_change(ctx.db, ctx.source.id, it.payload["device_id"], "role", want)
                results[it.id] = ExecResult(
                    success=verified is not False,
                    detail=(f"role set to {want} and verified" if verified else
                            "role set (verification unavailable)" if verified is None else
                            f"task succeeded but the role is {actual}"),
                    verified=verified, data={"from": it.payload.get("current_role"), "to": want})
        return results
