"""AP power shortfall: access points whose switch port allocates less PoE than the AP model needs.

An AP on too little power still joins and looks healthy, but it switches off radios or spatial streams,
disables its USB port or runs in a reduced mode. Users notice weak coverage or low throughput in one area.

The task maps every AP to its switch port with the Catalyst Center physical topology, reads those switches'
PoE port details from the Catalyst Center API and compares the allocated power with the power the AP model
needs for full function. Findings go to a report grouped per switch; Steward never changes switch ports.
"""
import re
from collections import Counter

from app.connectors.catalyst_center.client import CapabilityUnavailable
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh
from app.models import WorkItem
from app.usecases.ap_uplink import ap_uplinks, port_key
from app.usecases.common import fail_all, open_targets

# Power at the switch port (W) for full function, by platform prefix. 15.4 = 802.3af, 30 = 802.3at (PoE+),
# 45 = 802.3bt class 5, 60 = 802.3bt class 6 / UPOE. Check against the data sheets of the models in use.
REQUIRED_W = {
    "C9105": 15.4, "C9115": 30.0, "C9117": 45.0, "C9120": 30.0, "C9124": 45.0, "C9130": 45.0, "C9136": 60.0,
    "C9162": 30.0, "C9164": 30.0, "C9166": 45.0, "C9172": 30.0, "C9176": 45.0, "C9178": 60.0,
    "AIR-AP1832": 15.4, "AIR-AP1852": 30.0, "AIR-AP2802": 30.0, "AIR-AP3802": 30.0, "AIR-AP4800": 45.0,
}
DENIED = ("faulty", "power_deny", "powerdeny", "deny", "denied")
LABELS = {"derated": "Too little power", "denied": "Power denied"}
ADVICE = {
    "derated": ("Give the AP the power it needs: a UPOE/802.3bt switch port, LLDP enabled on the port so the AP "
                "can negotiate more than 802.3at, or a power injector. Disabling the AP's USB port lowers the need."),
    "denied": ("The switch refuses power on this port (power budget exhausted or a fault). Check the switch power "
               "supplies and budget, then the cable."),
}


def watts_table(overrides: str | None) -> dict[str, float]:
    """Built-in table with operator overrides like "C9130=30, CW9166=45" on top."""
    table = dict(REQUIRED_W)
    for part in str(overrides or "").replace(";", ",").split(","):
        model, sep, watts = part.partition("=")
        try:
            if sep and model.strip():
                table[re.sub(r"^CW", "C", model.strip().upper())] = float(watts)
        except ValueError:
            continue
    return table


def required_watts(platform: str | None, table: dict[str, float]) -> float | None:
    model = re.sub(r"^CW", "C", (platform or "").split(",")[0].strip().upper())
    for prefix in sorted(table, key=len, reverse=True):
        if model.startswith(prefix.upper()):
            return float(table[prefix])
    return None


def classify(port, need_w: float, tolerance_w: float = 0.5) -> str | None:
    if any(w in (port.oper_status or "").lower().replace(" ", "_") for w in DENIED):
        return "denied"
    if (port.oper_status or "").lower() != "on" or port.allocated_w is None:
        return None
    return "derated" if port.allocated_w + tolerance_w < need_w else None


@register
class ApPowerShortfall(TaskDefinition):
    key = "ap_power_shortfall"
    name = "AP power shortfall"
    description = ("Finds access points whose switch port allocates less PoE than the AP model needs for full "
                   "function, so radios or spatial streams are switched off. Reads the switch PoE details through "
                   "the Catalyst Center API and reports per switch; Steward does not change switch ports.")
    category = "Wireless / Health"
    worker = "health"
    default_cron = "55 */6 * * *"
    max_autonomy = 0
    default_autonomy_level = 0
    default_autonomy_cap = 0
    action_types = {"report": "Owner report (no change)"}
    params = [
        ParamSpec("required_watts", "Power per AP model, overriding the built-in table (e.g. C9130=30, C9166=45)",
                  "str", "",
                  "Watts at the switch port for full function. Longest matching model prefix wins; CW models match "
                  "the C prefix. Built-in: " + ", ".join(f"{k}={v:g}" for k, v in REQUIRED_W.items())),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        table = watts_table(p.get("required_watts"))
        by_id = {d.id: d for d in inv.devices}
        site_label = {s.id: s.hierarchy for s in inv.sites}

        proposals: list[Proposal] = []
        checked: set[str] = set()
        stats = Counter()
        unknown: Counter = Counter()
        for sw_id, ports in ap_uplinks(inv).items():
            sw = by_id[sw_id]
            try:
                poe = ctx.cc.get_poe_ports(sw_id)
            except CapabilityUnavailable as exc:
                ctx.log(f"PoE API not available on this Catalyst Center: {exc}")
                stats["api_unavailable"] += 1
                break
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"{sw.hostname}: PoE read failed: {exc}")
                stats["switch_errors"] += 1
                continue
            if poe is None:
                stats["switch_no_data"] += 1
                continue
            stats["switches"] += 1
            by_port = {port_key(x.name): x for x in poe}
            for sw_port, ap_id in ports:
                ap = by_id[ap_id]
                port = by_port.get(port_key(sw_port))
                if port is None:
                    stats["port_not_found"] += 1
                    continue
                need = required_watts(ap.platform, table)
                if need is None:
                    unknown[(ap.platform or "unknown").split(",")[0]] += 1
                    continue
                checked.add(ap_id)
                stats["ports"] += 1
                cls = classify(port, need)
                if cls:
                    stats[cls] += 1
                    site = site_label.get(inv.assignments.get(ap_id), "")
                    proposals.append(self._proposal(ap, sw, port, cls, need, site))

        if unknown:
            ctx.log("AP models without a power requirement (add them to required_watts): "
                    + ", ".join(f"{m} ({n})" for m, n in unknown.most_common(10)))
        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"power_ok": True} for t in open_targets(ctx, self.key) if t in checked and t not in keys}
        ctx.log(f"Checked PoE on {stats['ports']} AP ports on {stats['switches']} switches: "
                f"{stats['derated']} with too little power, {stats['denied']} denied")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "power_shortfall": len(proposals), "unknown_models": sum(unknown.values()),
            "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, ap, sw, port, cls: str, need: float, site: str) -> Proposal:
        alloc = f"{port.allocated_w:g} W" if port.allocated_w is not None else "no power"
        text = f"{sw.hostname} {port.name}: {alloc} allocated, {ap.platform} needs {need:g} W"
        return Proposal(
            target_key=ap.id, target_label=f"{ap.hostname} ({ap.mgmt_ip or 'no ip'})",
            action_type="report",
            payload={"ap_id": ap.id, "hostname": ap.hostname, "class": cls, "site": site, "model": ap.platform,
                     "switch_id": sw.id, "switch": sw.hostname, "port": port.name, "allocated_w": port.allocated_w,
                     "drawn_w": port.drawn_w, "max_port_w": port.max_w, "required_w": need,
                     "oper_status": port.oper_status, "advice": ADVICE[cls]},
            rule_confidence=90 if cls == "derated" else 95,
            evidence=[{"signal": cls, "label": LABELS[cls], "available": True, "weight": 1.0, "detail": text,
                       "buildings": [], "floors": [], "agrees": True}],
            rationale=f"{ADVICE[cls]} {text}",
            blocked_reason="Owner action needed: Steward does not change switch ports.",
            group_label=sw.hostname,
            fingerprint=f"{cls}:{ap.id}:{sw.id}:{port.name}:{port.allocated_w}:{need}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return fail_all(items, "owner report: nothing to execute")
