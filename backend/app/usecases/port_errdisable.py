"""Repeat err-disabled ports: switch ports to APs, switches and controllers that keep going down.

The usual loop: a port is err-disabled (BPDU guard, port security, a PoE fault), someone re-enables it, and a
day later it is down again; every time is a new ticket. The task reads the interfaces of every switch port
that has a neighbour in the Catalyst Center topology (an AP, switch or controller, so the port should always
be up), and records each time such a port goes down while administratively up. Ports that went down
repeatedly become one report item with the count, instead of one ticket per occurrence.

Ports without a known neighbour (desks, printers) are left out: they go down every time someone unplugs.
Catalyst Center does not give the err-disable reason through its API, so the item lists the likely causes.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.connectors.catalyst_center.client import CapabilityUnavailable
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, history, is_unreachable
from app.models import DeviceStateHistory, WorkItem
from app.store import cache_get, cache_set
from app.usecases.ap_uplink import port_key
from app.usecases.common import fail_all, open_targets

FIELD = "port_down"
STATE_TTL = 30 * 86_400
ADVICE = ("Find the cause before re-enabling the port again: the switch log or 'show interfaces status "
          "err-disabled' names it. Common causes: BPDU guard (a switch or loop behind the port), port security, "
          "an inline power (PoE) fault, UDLD or a link that keeps flapping (cable, optic, patch panel).")


def port_state(intf) -> str:
    status = (intf.status or "").lower()
    if (intf.admin_status or "up").lower() == "down":
        return "admin_down"
    if "err" in status:
        return "err"
    return "up" if status == "up" else "down"


def infra_ports(inv) -> dict[str, dict[str, tuple[str, str]]]:
    """Switch id -> {port key: (port name, neighbour id)} for ports with a neighbour in the topology."""
    by_id = {d.id: d for d in inv.devices}
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for link in inv.links:
        for dev, port, other in ((link.a_id, link.a_port, link.b_id), (link.b_id, link.b_port, link.a_id)):
            d = by_id.get(dev)
            if d and port and other in by_id and (d.family or "").lower() == "switches and hubs":
                out.setdefault(dev, {})[port_key(port)] = (port, other)
    return out


@register
class RepeatErrDisabledPorts(TaskDefinition):
    key = "repeat_errdisabled_ports"
    name = "Repeat err-disabled ports"
    description = ("Finds switch ports to APs, switches and controllers that keep going down while enabled "
                   "(err-disabled, re-enabled, err-disabled again). Reads the switch interfaces through the "
                   "Catalyst Center API on every run and reports each port once with the count and the likely "
                   "causes; Steward does not change switch ports.")
    category = "Health / Switching"
    worker = "health"
    default_cron = "5 * * * *"
    max_autonomy = 0
    default_autonomy_level = 0
    default_autonomy_cap = 0
    action_types = {"report": "Owner report (no change)"}
    params = [
        ParamSpec("window_days", "Look-back window (days)", "int", 7),
        ParamSpec("min_episodes", "Report a port after this many times down in the window", "int", 3),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        now = datetime.now(timezone.utc)
        by_id = {d.id: d for d in inv.devices}
        site_label = {s.id: s.hierarchy for s in inv.sites}
        key = f"port_state:{ctx.source.id}"
        states: dict[str, dict[str, str]] = dict(cache_get(ctx.db, key) or {})
        stats = Counter()
        current: dict[tuple[str, str], str] = {}
        for sw_id, ports in infra_ports(inv).items():
            sw = by_id[sw_id]
            if is_unreachable(sw.reachability):
                stats["switch_unreachable"] += 1
                continue
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
            prev = states.get(sw_id) or {}
            seen: dict[str, str] = {}
            for intf in intfs:
                pk = port_key(intf.name)
                if pk not in ports:
                    continue
                state = seen[pk] = current[(sw_id, pk)] = port_state(intf)
                stats["ports"] += 1
                was = prev.get(pk)
                if state in ("down", "err") and (was == "up" or (was is None and state == "err")):
                    stats["went_down"] += 1
                    ctx.db.add(DeviceStateHistory(source_id=ctx.source.id, device_id=sw_id, ts=now, field=FIELD,
                                                  old="err-disabled" if state == "err" else None, new=intf.name))
            states[sw_id] = seen
        cache_set(ctx.db, key, states, STATE_TTL)
        ctx.db.flush()

        window = int(p.get("window_days", 7))
        min_episodes = int(p.get("min_episodes", 3))
        episodes: dict[tuple[str, str], list[DeviceStateHistory]] = {}
        for sw_id, rows in history(ctx.db, ctx.source.id, FIELD, now - timedelta(days=window)).items():
            for h in rows:
                episodes.setdefault((sw_id, port_key(h.new)), []).append(h)
        ports_by_switch = infra_ports(inv)
        proposals: list[Proposal] = []
        for (sw_id, pk), rows in episodes.items():
            if len(rows) < min_episodes or sw_id not in by_id or pk not in ports_by_switch.get(sw_id, {}):
                continue
            name, other = ports_by_switch[sw_id][pk]
            site = site_label.get(inv.assignments.get(sw_id), "")
            proposals.append(self._proposal(by_id[sw_id], name, by_id.get(other), rows, current.get((sw_id, pk)),
                                            site, window))
        stats["repeat_ports"] = len(proposals)

        keys = {pr.target_key for pr in proposals}
        resolved = {t: {"repeat": False} for t in open_targets(ctx, self.key) if t not in keys}
        ctx.log(f"Checked {stats['ports']} infrastructure ports on {stats['switches']} switches: "
                f"{stats['went_down']} went down since the last run, {len(proposals)} down {min_episodes}+ times "
                f"in {window} days")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "cc_api_calls": ctx.cc.http.calls, **stats})

    def _proposal(self, sw, port: str, nbr, rows, state: str | None, site: str, window: int) -> Proposal:
        errdis = any(h.old == "err-disabled" for h in rows)
        nbr_label = f"{nbr.hostname} ({nbr.family})" if nbr else "unknown neighbour"
        text = (f"{sw.hostname} {port} to {nbr_label}: down {len(rows)} times in {window} days, "
                f"last {rows[-1].ts:%Y-%m-%d %H:%M} UTC; now {state or 'unknown'}")
        return Proposal(
            target_key=f"{sw.id}:{port_key(port)}", target_label=f"{sw.hostname} {port}",
            action_type="report",
            payload={"switch_id": sw.id, "switch": sw.hostname, "port": port, "site": site, "count": len(rows),
                     "window_days": window, "state": state, "err_disabled_seen": errdis,
                     "neighbour": nbr.hostname if nbr else None, "neighbour_family": nbr.family if nbr else None,
                     "times": [h.ts.isoformat() for h in rows][-10:], "advice": ADVICE},
            rule_confidence=95 if errdis else 85,
            evidence=[{"signal": "port_state", "label": "Port went down repeatedly", "available": True,
                       "weight": 1.0, "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=f"{ADVICE} {text}",
            blocked_reason="Owner action needed: Steward does not change switch ports.",
            group_label=sw.hostname,
            fingerprint=f"errdis:{sw.id}:{port_key(port)}:{len(rows)}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return fail_all(items, "owner report: nothing to execute")
