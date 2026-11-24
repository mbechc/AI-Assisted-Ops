"""AP uplink health: access points behind a switch port that negotiated half duplex or a low speed.

A wrongly configured or damaged switch port (or cable) often leaves an AP at 100 Mbit half duplex. The AP
stays up, so nothing alarms, but clients get poor throughput and the fault is hard to find by hand.

The task maps every AP to its switch port with the Catalyst Center physical topology and reads only those
switches' interfaces from the Catalyst Center interface API. Findings go to a report grouped per switch;
Steward never changes switch ports.
"""
import re
from collections import Counter

from app.connectors.catalyst_center.client import CapabilityUnavailable
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh
from app.models import WorkItem
from app.usecases.common import fail_all, open_targets

LABELS = {"half_duplex": "Half duplex", "low_speed": "Low speed"}
ADVICE = {
    "half_duplex": ("Check the switch port and the cable. Half duplex usually means one side is hard-coded to a "
                    "speed/duplex while the other auto-negotiates, or a damaged cable or patch panel. Set both "
                    "sides to auto, then bounce the port."),
    "low_speed": ("Check the cable and patch panel for the AP (two broken pairs give 100 Mbit), and that the "
                  "switch port is not hard-coded to a lower speed."),
}


def port_key(name: str | None) -> str:
    """Comparable port name: 'GigabitEthernet1/0/5' and 'Gi1/0/5' both become 'gi1/0/5'."""
    s = (name or "").replace(" ", "").lower()
    m = re.match(r"([a-z-]+)([\d/.:]+)$", s)
    return f"{m.group(1)[:2]}{m.group(2)}" if m else s


def ap_uplinks(inv) -> dict[str, list[tuple[str, str]]]:
    """Switch id -> [(switch port, AP id)] from the physical topology in the inventory snapshot."""
    by_id = {d.id: d for d in inv.devices}
    out: dict[str, list[tuple[str, str]]] = {}
    for link in inv.links:
        a, b = by_id.get(link.a_id), by_id.get(link.b_id)
        if not a or not b or a.is_ap == b.is_ap:
            continue
        sw, sw_port, ap = (a, link.a_port, b) if b.is_ap else (b, link.b_port, a)
        if sw_port:
            out.setdefault(sw.id, []).append((sw_port, ap.id))
    return out


def classify(intf, min_speed_mbps: int) -> str | None:
    if "half" in (intf.duplex or "").lower():
        return "half_duplex"
    if intf.speed_mbps is not None and intf.speed_mbps < min_speed_mbps:
        return "low_speed"
    return None


@register
class ApUplinkHealth(TaskDefinition):
    key = "ap_uplink_health"
    name = "AP uplink health"
    description = ("Finds access points whose switch port runs half duplex or below the expected speed "
                   "(the classic 100 Mbit half duplex AP). Reads the switch interfaces through the Catalyst "
                   "Center API and reports per switch; Steward does not change switch ports.")
    category = "Wireless / Health"
    worker = "health"
    default_cron = "50 */6 * * *"
    max_autonomy = 0
    default_autonomy_level = 0
    default_autonomy_cap = 0
    action_types = {"report": "Owner report (no change)"}
    params = [
        ParamSpec("min_speed_mbps", "Flag AP ports below this speed (Mbit/s)", "int", 1000),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        min_speed = int(p.get("min_speed_mbps", 1000))
        by_id = {d.id: d for d in inv.devices}
        site_label = {s.id: s.hierarchy for s in inv.sites}

        uplinks = ap_uplinks(inv)

        proposals: list[Proposal] = []
        checked: set[str] = set()
        stats = Counter()
        for sw_id, ports in uplinks.items():
            sw = by_id[sw_id]
            try:
                intfs = ctx.cc.get_interfaces(sw_id)
            except CapabilityUnavailable as exc:
                ctx.log(f"Interface API not available on this Catalyst Center: {exc}")
                stats["api_unavailable"] += 1
                break
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"{sw.hostname}: interface read failed: {exc}")
                stats["switch_errors"] += 1
                continue
            if intfs is None:
                stats["switch_no_data"] += 1
                continue
            stats["switches"] += 1
            by_port = {port_key(i.name): i for i in intfs}
            for sw_port, ap_id in ports:
                checked.add(ap_id)
                intf = by_port.get(port_key(sw_port))
                if intf is None:
                    stats["port_not_found"] += 1
                    continue
                if (intf.status or "").lower() != "up":
                    stats["port_down"] += 1
                    continue
                stats["ports"] += 1
                cls = classify(intf, min_speed)
                if cls:
                    stats[cls] += 1
                    site = site_label.get(inv.assignments.get(ap_id), "")
                    proposals.append(self._proposal(by_id[ap_id], sw, intf, cls, site, min_speed))

        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"uplink_ok": True} for t in open_targets(ctx, self.key) if t in checked and t not in keys}
        ctx.log(f"Checked {stats['ports']} AP ports on {stats['switches']} switches: "
                f"{stats['half_duplex']} half duplex, {stats['low_speed']} below {min_speed} Mbit")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "bad_uplinks": len(proposals), "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, ap, sw, intf, cls: str, site: str, min_speed: int) -> Proposal:
        speed = f"{intf.speed_mbps} Mbit" if intf.speed_mbps else "unknown speed"
        duplex = "half duplex" if cls == "half_duplex" else (intf.duplex or "duplex unknown")
        text = f"{sw.hostname} {intf.name}: {speed}, {duplex}"
        return Proposal(
            target_key=ap.id, target_label=f"{ap.hostname} ({ap.mgmt_ip or 'no ip'})",
            action_type="report",
            payload={"ap_id": ap.id, "hostname": ap.hostname, "class": cls, "site": site,
                     "switch_id": sw.id, "switch": sw.hostname, "port": intf.name, "speed_mbps": intf.speed_mbps,
                     "duplex": intf.duplex, "min_speed_mbps": min_speed, "description": intf.description,
                     "advice": ADVICE[cls]},
            rule_confidence=95 if cls == "half_duplex" else 90,
            evidence=[{"signal": cls, "label": LABELS[cls], "available": True, "weight": 1.0, "detail": text,
                       "buildings": [], "floors": [], "agrees": True}],
            rationale=f"{ADVICE[cls]} {text}",
            blocked_reason="Owner action needed: Steward does not change switch ports.",
            group_label=sw.hostname,
            fingerprint=f"{cls}:{ap.id}:{sw.id}:{intf.name}:{intf.speed_mbps}:{intf.duplex}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return fail_all(items, "owner report: nothing to execute")
