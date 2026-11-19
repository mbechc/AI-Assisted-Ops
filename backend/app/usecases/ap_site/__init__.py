"""Use case 1: suggest (and optionally assign) a site for access points that have none.

All data comes from, and all changes go through, the Catalyst Center API. The
platform never talks to WLCs or APs directly.
"""
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict

from app.connectors.catalyst_center.models import Device, Site
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh, record_change
from app.models import WorkItem
from app.store import cache_get, cache_set
from app.usecases.ap_site.learners import SiteIndex
from app.usecases.ap_site.scoring import score
from app.usecases.ap_site.signals import SiteSignals, llm_evidence_lines

LLM_SYSTEM = (
    "You review proposed Cisco Catalyst Center site assignments for wireless access points (APs). "
    "You receive the AP, the proposed site (Area/Building/Floor) and the evidence signals with their weights. "
    "Judge how likely the proposed site is correct. Strong evidence: the uplink switch and RF neighbours agree "
    "on the building and floor. Weak evidence: a subnet, GeoIP or naming match alone. Lower the confidence when "
    "signals conflict, when naming points elsewhere, or when the floor is a guess. Never invent data. "
    "Return JSON with confidence (0-100), a rationale of at most three sentences, and a list of concrete concerns."
)

SIGNAL_PARAMS = [
    ParamSpec("weight_uplink", "Weight: wired uplink", "float", 0.40),
    ParamSpec("weight_rf", "Weight: RF neighbours", "float", 0.30),
    ParamSpec("weight_name", "Weight: name convention", "float", 0.15),
    ParamSpec("weight_tag", "Weight: AP site tag / location", "float", 0.10),
    ParamSpec("weight_subnet", "Weight: management subnet", "float", 0.05),
    ParamSpec("weight_geoip", "Weight: GeoIP (only used when a GeoIP source is configured)", "float", 0.15),
    ParamSpec("weight_ai_naming", "Weight: AI-learned naming convention (only with AI on; max 0.15)", "float", 0.10),
    ParamSpec("ai_naming_enabled", "AI: learn the naming convention (only with AI on)", "bool", True),
    ParamSpec("name_min_samples", "Name model: minimum samples", "int", 3),
    ParamSpec("name_min_purity", "Name model: minimum purity", "float", 0.8),
    ParamSpec("ap_config_enabled", "Use AP configuration (site tag / location)", "bool", True),
    ParamSpec("ap_config_learning_sample", "AP configs to learn from (assigned APs)", "int", 1500),
    ParamSpec("ap_config_cache_hours", "AP configuration cache (hours)", "int", 168),
    ParamSpec("rf_enabled", "Use RF neighbours", "bool", True),
    ParamSpec("rf_cache_hours", "RF neighbour cache (hours)", "int", 20),
    ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
]
EXEC_PARAMS = [
    ParamSpec("assign_batch_size", "Devices per assign-to-site call", "int", 25),
    ParamSpec("task_timeout_seconds", "Catalyst Center task timeout (s)", "int", 900),
]
LLM_PARAMS = [
    ParamSpec("llm_enabled", "LLM review enabled", "bool", True),
    ParamSpec("llm_min_confidence", "LLM review: min rule confidence", "int", 35),
    ParamSpec("llm_max_confidence", "LLM review: max rule confidence", "int", 90),
    ParamSpec("llm_max_items", "LLM review: max items per run", "int", 150),
]


def sites_cache_key(source_id: int | None) -> str:
    return f"cc:sites:{source_id}"


def fingerprint(sc) -> str:
    src = json.dumps({"site": sc.site_id, "ev": [(e["signal"], e["buildings"], e["floors"]) for e in sc.evidence]},
                     sort_keys=True, default=str)
    return hashlib.sha256(src.encode()).hexdigest()


def removed_targets(ctx: RunContext, task_key: str, current_ids: set[str]) -> dict[str, dict]:
    from app.usecases.common import open_targets
    return {t: {"removed_from_inventory": True} for t in open_targets(ctx, task_key) if t not in current_ids}


def site_override(ctx: RunContext, item: WorkItem, override: dict, require_floor: bool) -> dict:
    site_id = override.get("site_id")
    if not site_id:
        raise ValueError("override requires site_id")
    sites = cache_get(ctx.db, sites_cache_key(item.source_id)) or []
    site = next((s for s in sites if s["id"] == site_id), None)
    if site is None:
        raise ValueError("unknown site; run the task again to refresh the site hierarchy")
    if require_floor and site["type"] != "floor":
        raise ValueError("access points must be assigned to a floor")
    if site["type"] not in ("building", "floor"):
        raise ValueError("devices can only be assigned to a building or a floor")
    return {"site_id": site["id"], "site_hierarchy": site["hierarchy"], "site_type": site["type"]}


def execute_assignments(defn: TaskDefinition, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
    """Assign devices to their (effective) target site via Catalyst Center and verify membership."""
    cc = ctx.cc
    batch_size = max(1, int(ctx.params.get("assign_batch_size", 25)))
    timeout = int(ctx.params.get("task_timeout_seconds", 900))
    by_site: dict[str, list[WorkItem]] = defaultdict(list)
    results: dict[int, ExecResult] = {}
    for item in items:
        pl = defn.effective_payload(item)
        if not pl.get("site_id"):
            results[item.id] = ExecResult(False, "no target site")
            continue
        by_site[pl["site_id"]].append(item)

    for site_id, site_items in by_site.items():
        pl0 = defn.effective_payload(site_items[0])
        site = Site(id=site_id, name=(pl0.get("site_hierarchy") or "").split("/")[-1], parent_id=None,
                    hierarchy=pl0.get("site_hierarchy") or "", type=pl0.get("site_type") or "floor")
        for i in range(0, len(site_items), batch_size):
            batch = site_items[i:i + batch_size]
            devices = [Device(id=it.payload["device_id"], hostname=it.payload.get("hostname") or "",
                              mgmt_ip=it.payload.get("mgmt_ip"), mac=it.payload.get("mac"),
                              family=it.payload.get("family") or "Unified AP")
                       for it in batch]
            ctx.log(f"Assigning {len(devices)} device(s) to {site.hierarchy}")
            try:
                handle = cc.assign_devices_to_site(site, devices)
                res = cc.wait_for(handle, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                for it in batch:
                    results[it.id] = ExecResult(False, f"assign failed: {exc}")
                continue
            if not res.success:
                for it in batch:
                    results[it.id] = ExecResult(False, f"Catalyst Center task failed: {res.detail}",
                                                data={"task": res.raw})
                continue
            try:
                members = cc.get_site_assignments([site])
            except Exception as exc:  # noqa: BLE001
                members = None
                ctx.log(f"verification read failed: {exc}")
            for it, dev in zip(batch, devices):
                verified = None if members is None else members.get(dev.id) == site_id
                if verified:
                    record_change(ctx.db, ctx.source.id, dev.id, "site_id", site_id)
                results[it.id] = ExecResult(
                    success=verified is not False,
                    detail="assigned and verified" if verified else (
                        "assigned (verification unavailable)" if verified is None
                        else "task succeeded but device is not a member of the site"),
                    verified=verified,
                    data={"site_id": site_id, "site": site.hierarchy, "handle": asdict(handle)},
                )
    return results


@register
class ApSiteAssignment(TaskDefinition):
    key = "ap_site_assignment"
    name = "AP site assignment"
    description = ("Finds access points without a site in Catalyst Center and proposes a floor (or building) "
                   "using the wired uplink (CDP/LLDP), RF neighbours, AP naming, AP site tag/location, "
                   "management subnet and, when configured, GeoIP. Approved proposals are assigned via the "
                   "Catalyst Center API.")
    category = "Wireless / Inventory hygiene"
    worker = "wireless"
    optional_sources = ("geoip",)
    default_cron = "30 5 * * *"
    max_autonomy = 3
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 100
    action_types = {"assign_site": "Assign device to site (no provisioning, no config push to the WLC)"}
    llm_system_prompt = LLM_SYSTEM
    ai_features = {
        "proposal_review": "Second opinion on mid-confidence proposals (can lower confidence, raise it by at most 5)",
        "naming_convention": "Learns the site naming convention from assigned devices as an extra evidence signal",
    }
    params = SIGNAL_PARAMS + [
        ParamSpec("ap_require_floor", "APs must be assigned to a floor", "bool", True,
                  "When the floor cannot be determined the item requires an operator floor choice."),
        ParamSpec("max_aps_per_run", "Max unassigned APs analysed per run (0 = all)", "int", 0),
    ] + EXEC_PARAMS + LLM_PARAMS

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p, cc = ctx.params, ctx.cc
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        index = SiteIndex(inv.sites)
        cache_set(ctx.db, sites_cache_key(ctx.source.id), [s.to_dict() for s in inv.sites], 30 * 86400)
        ctx.log(f"Sites: {len(inv.sites)} ({sum(s.type == 'building' for s in inv.sites)} buildings, "
                f"{sum(s.type == 'floor' for s in inv.sites)} floors)")

        assignments = inv.assignments
        aps = [d for d in inv.devices if d.is_ap]
        assigned_aps = [a for a in aps if a.id in assignments]
        unassigned = [a for a in aps if a.id not in assignments]
        ctx.log(f"Devices: {len(inv.devices)}, APs: {len(aps)}, APs without site: {len(unassigned)}")
        if p.get("max_aps_per_run"):
            unassigned = unassigned[: int(p["max_aps_per_run"])]

        sig = SiteSignals(ctx, inv, index, p, unassigned)
        require_floor = bool(p["ap_require_floor"])
        proposals: list[Proposal] = []
        stats = Counter()
        for ap in unassigned:
            sc = score(sig.votes(ap), sig.available, sig.weights, index, require_floor)
            proposals.append(self._proposal(ap, sc, index, require_floor))
            stats["proposals"] += 1
            stats["floor_determined" if sc.floor_determined else
                  ("no_evidence" if sc.building_id is None else "floor_ambiguous")] += 1

        resolved = {a.id: {"actual_site_id": assignments[a.id], "actual_site": index.label(assignments[a.id]),
                           "actual_building_id": index.building_of(assignments[a.id])}
                    for a in assigned_aps}
        resolved.update(removed_targets(ctx, self.key, {a.id for a in aps}))
        sig.log_available()
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "aps_total": len(aps), "aps_unassigned": len([a for a in aps if a.id not in assignments]),
            "signals_available": sig.available, "cc_api_calls": cc.http.calls, **stats,
        })

    def _proposal(self, ap: Device, sc, index: SiteIndex, require_floor: bool) -> Proposal:
        site = index.get(sc.site_id)
        blocked = None
        if sc.site_id is None:
            blocked = "No evidence found: choose a site manually."
        elif require_floor and (site is None or site.type != "floor"):
            blocked = "Floor could not be determined: choose one of the candidate floors."
        payload = {
            "device_id": ap.id, "hostname": ap.hostname, "mgmt_ip": ap.mgmt_ip, "mac": ap.mac,
            "platform": ap.platform, "wlc_ip": ap.wlc_ip, "family": ap.family,
            "site_id": sc.site_id, "site_hierarchy": site.hierarchy if site else None,
            "site_type": site.type if site else None,
            "building_id": sc.building_id, "building": index.label(sc.building_id) if sc.building_id else None,
            "floor_determined": sc.floor_determined, "candidate_floors": sc.candidate_floors,
            "auto_eligible": sc.auto_eligible,
        }
        lines = [e["detail"] for e in sc.evidence if e["agrees"]]
        if sc.site_id:
            rationale = f"Proposed {index.short(sc.site_id, 3)}. " + ("; ".join(lines) + "." if lines else "")
        else:
            rationale = "No signal identified a site for this AP."
        return Proposal(
            target_key=ap.id,
            target_label=f"{ap.hostname} ({ap.mgmt_ip or 'no ip'})",
            action_type="assign_site",
            payload=payload,
            rule_confidence=sc.confidence,
            evidence=sc.evidence,
            rationale=rationale,
            concerns=sc.concerns,
            group_label=index.label(sc.building_id) if sc.building_id else "No evidence",
            blocked_reason=blocked,
            llm_prompt=self._llm_prompt(ap, sc, index) if sc.site_id else None,
            fingerprint=fingerprint(sc),
        )

    @staticmethod
    def _llm_prompt(ap: Device, sc, index: SiteIndex) -> str:
        lines = [
            f"AP: {ap.hostname}, model {ap.platform or 'unknown'}, IP {ap.mgmt_ip or 'unknown'}, WLC {ap.wlc_ip or 'unknown'}",
            f"Proposed site: {index.label(sc.site_id)} (floor determined: {'yes' if sc.floor_determined else 'no'})",
            f"Rule-based confidence: {sc.confidence}",
            "Evidence:",
        ] + llm_evidence_lines(sc, index)
        others = sorted(sc.building_scores.items(), key=lambda kv: -kv[1])[1:3]
        if others:
            lines.append("Runner-up buildings: " + ", ".join(f"{index.short(b)} ({s:.2f})" for b, s in others))
        if sc.candidate_floors and not sc.floor_determined:
            lines.append("Candidate floors: " + ", ".join(f"{c['hierarchy']} ({c['score']})" for c in sc.candidate_floors[:4]))
        return "\n".join(lines)

    def apply_override(self, ctx: RunContext, item: WorkItem, override: dict) -> dict:
        return site_override(ctx, item, override, bool((ctx.params or {}).get("ap_require_floor", True)))

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        if observed.get("removed_from_inventory"):
            return {**observed, "matched": None}
        proposed = (item.payload or {}).get("site_id")
        actual = observed.get("actual_site_id")
        return {
            **observed,
            "proposed_site_id": proposed,
            "matched": None if not proposed else actual == proposed,
            "building_matched": None if not proposed else observed.get("actual_building_id") == (item.payload or {}).get("building_id"),
        }

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return execute_assignments(self, ctx, items)
