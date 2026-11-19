"""Wrong-site detection: devices whose evidence strongly points to another building (or floor).

Uses the same signal engine as AP site assignment, on devices that already have a site. Works
fully without GeoIP; when a GeoIP source is configured it adds one more vote, and a mismatch
supported only by GeoIP is never executed automatically.
"""
from collections import Counter

from app.connectors.catalyst_center.models import Device
from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh
from app.models import WorkItem
from app.store import cache_set
from app.usecases.ap_site import (
    EXEC_PARAMS, LLM_PARAMS, SIGNAL_PARAMS, execute_assignments, fingerprint, removed_targets, site_override,
    sites_cache_key,
)
from app.usecases.ap_site.learners import SiteIndex
from app.usecases.ap_site.scoring import score
from app.usecases.ap_site.signals import SiteSignals, llm_evidence_lines

LLM_SYSTEM = (
    "You review proposals to move a network device that already has a site in Cisco Catalyst Center to a different "
    "site, because evidence suggests the current site is wrong. You receive the device, its current site, the "
    "proposed site and the evidence signals with weights. Moving a correctly placed device is harmful, so be "
    "sceptical: require the uplink, RF neighbours or naming to agree on the proposed site. Lower the confidence "
    "when the current site still has supporting evidence or when only subnet or GeoIP supports the move. "
    "Return JSON with confidence (0-100), a rationale of at most three sentences, and a list of concrete concerns."
)

SWITCH_FAMILIES = ("switches and hubs",)


@register
class WrongSiteDetection(TaskDefinition):
    key = "site_reassignment"
    name = "Wrong-site detection"
    description = ("Checks devices that already have a site in Catalyst Center against the uplink (CDP/LLDP), RF "
                   "neighbours, naming, AP site tag, subnet and (optional) GeoIP evidence. When the evidence "
                   "clearly points to another building or floor, it proposes a reassignment via the Catalyst "
                   "Center API. Works without GeoIP.")
    category = "Inventory hygiene"
    worker = "inventory"
    optional_sources = ("geoip",)
    default_cron = "45 5 * * *"
    max_autonomy = 2
    default_autonomy_level = 1
    default_autonomy_cap = 1
    default_auto_threshold = 95
    default_max_auto_per_run = 20
    action_types = {"reassign_site": "Move device to another site (no provisioning, no config push)"}
    llm_system_prompt = LLM_SYSTEM
    ai_features = {
        "proposal_review": "Second opinion on mid-confidence moves (can lower confidence, raise it by at most 5)",
        "naming_convention": "Learns the site naming convention from assigned devices as an extra evidence signal",
    }
    params = SIGNAL_PARAMS + [
        ParamSpec("include_aps", "Check access points", "bool", True),
        ParamSpec("include_switches", "Check switches", "bool", True),
        ParamSpec("min_confidence", "Minimum confidence to report a mismatch", "int", 40),
        ParamSpec("min_agreeing_signals", "Minimum independent signals agreeing on the new building", "int", 2),
        ParamSpec("floor_check_enabled", "Also check the floor of APs inside the correct building", "bool", True),
        ParamSpec("floor_min_confidence", "Minimum confidence for a floor-only mismatch", "int", 70),
    ] + EXEC_PARAMS + LLM_PARAMS

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        index = SiteIndex(inv.sites)
        cache_set(ctx.db, sites_cache_key(ctx.source.id), [s.to_dict() for s in inv.sites], 30 * 86400)
        assignments = inv.assignments

        def wanted(d: Device) -> bool:
            if d.id not in assignments:
                return False
            if d.is_ap:
                return bool(p.get("include_aps", True))
            return bool(p.get("include_switches", True)) and (d.family or "").lower() in SWITCH_FAMILIES

        targets = [d for d in inv.devices if wanted(d)]
        ctx.log(f"Devices with a site to check: {len(targets)} "
                f"({sum(d.is_ap for d in targets)} APs, {sum(not d.is_ap for d in targets)} switches)")
        sig = SiteSignals(ctx, inv, index, p, targets, name_learning="all")

        min_conf = int(p.get("min_confidence", 40))
        floor_min = int(p.get("floor_min_confidence", 70))
        min_agree = int(p.get("min_agreeing_signals", 2))
        proposals: list[Proposal] = []
        stats = Counter()
        for d in targets:
            cur = assignments[d.id]
            cur_b = index.building_of(cur)
            votes = sig.votes(d)
            sc = score(votes, sig.available, sig.weights, index, require_floor=d.is_ap)
            if sc.building_id is None:
                stats["no_evidence"] += 1
                continue
            agreeing = [e["signal"] for e in sc.evidence if e["agrees"]]
            if sc.building_id != cur_b:
                top = sc.building_scores.get(sc.building_id, 0.0) or 1.0
                cur_ratio = sc.building_scores.get(cur_b, 0.0) / top
                conf = int(round(sc.confidence * (1 - 0.8 * cur_ratio)))
                if len(agreeing) < min_agree or conf < min_conf:
                    stats["weak_mismatch"] += 1
                    continue
                kind = "building"
            elif (d.is_ap and p.get("floor_check_enabled", True) and sc.floor_determined
                  and sc.site_id != cur and index.get(cur) and index.get(cur).type == "floor"):
                conf = int(round(sc.confidence * 0.85))
                rf_floor = votes.get("rf") and votes["rf"].floors.get(sc.site_id, 0) >= 0.5
                if conf < floor_min or not rf_floor:
                    stats["weak_floor_mismatch"] += 1
                    continue
                kind = "floor"
            else:
                stats["consistent"] += 1
                continue
            stats[f"{kind}_mismatch"] += 1
            proposals.append(self._proposal(d, cur, sc, conf, kind, index))

        proposed = {pr.target_key for pr in proposals}
        resolved = {t: {"actual_site_id": assignments.get(t), "actual_site": index.label(assignments.get(t)),
                        "actual_building_id": index.building_of(assignments.get(t))}
                    for t in assignments if t not in proposed}
        resolved.update(removed_targets(ctx, self.key, {d.id for d in inv.devices}))
        sig.log_available()
        ctx.log(f"Mismatches: {stats['building_mismatch']} building, {stats['floor_mismatch']} floor; "
                f"{stats['consistent']} consistent, {stats['weak_mismatch'] + stats['weak_floor_mismatch']} "
                f"weak (not reported)")
        return AnalysisResult(proposals=proposals, resolved=resolved, stats={
            "devices_checked": len(targets), "signals_available": sig.available, "cc_api_calls": ctx.cc.http.calls,
            "proposals": len(proposals), **stats,
        })

    def _proposal(self, d: Device, cur: str, sc, conf: int, kind: str, index: SiteIndex) -> Proposal:
        site = index.get(sc.site_id)
        blocked = None
        if d.is_ap and (site is None or site.type != "floor"):
            blocked = "Floor in the proposed building could not be determined: choose one of the candidate floors."
        payload = {
            "device_id": d.id, "hostname": d.hostname, "mgmt_ip": d.mgmt_ip, "mac": d.mac, "family": d.family,
            "platform": d.platform, "mismatch": kind,
            "current_site_id": cur, "current_site": index.label(cur),
            "site_id": sc.site_id, "site_hierarchy": site.hierarchy if site else None,
            "site_type": site.type if site else None,
            "building_id": sc.building_id, "building": index.label(sc.building_id),
            "floor_determined": sc.floor_determined, "candidate_floors": sc.candidate_floors,
            "auto_eligible": sc.auto_eligible,
        }
        lines = [e["detail"] for e in sc.evidence if e["agrees"]]
        rationale = (f"Currently in {index.short(cur, 3)}; evidence points to {index.short(sc.site_id, 3)}. "
                     + ("; ".join(lines) + "." if lines else ""))
        concerns = list(sc.concerns)
        cur_score = sc.building_scores.get(index.building_of(cur), 0.0)
        if kind == "building" and cur_score > 0:
            concerns.append(f"The current building still has some support (score {cur_score:.2f}).")
        return Proposal(
            target_key=d.id,
            target_label=f"{d.hostname} ({d.mgmt_ip or 'no ip'})",
            action_type="reassign_site",
            payload=payload,
            rule_confidence=max(0, min(100, conf)),
            evidence=sc.evidence,
            rationale=rationale,
            concerns=concerns,
            group_label=f"{index.short(cur)} -> {index.short(sc.building_id)}",
            blocked_reason=blocked,
            llm_prompt=self._llm_prompt(d, cur, sc, index),
            fingerprint=fingerprint(sc),
        )

    @staticmethod
    def _llm_prompt(d: Device, cur: str, sc, index: SiteIndex) -> str:
        return "\n".join([
            f"Device: {d.hostname}, family {d.family}, model {d.platform or 'unknown'}, IP {d.mgmt_ip or 'unknown'}",
            f"Current site: {index.label(cur)}",
            f"Proposed site: {index.label(sc.site_id)}",
            f"Rule-based confidence: {sc.confidence}",
            "Evidence:",
        ] + llm_evidence_lines(sc, index))

    def apply_override(self, ctx: RunContext, item: WorkItem, override: dict) -> dict:
        return site_override(ctx, item, override, (item.payload or {}).get("family") == "Unified AP")

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        if observed.get("removed_from_inventory"):
            return {**observed, "matched": None}
        pl = item.payload or {}
        actual = observed.get("actual_site_id")
        if actual == pl.get("current_site_id"):
            return {**observed, "matched": None, "reason": "no longer detected as wrong"}
        return {**observed, "proposed_site_id": pl.get("site_id"), "matched": actual == pl.get("site_id"),
                "building_matched": observed.get("actual_building_id") == pl.get("building_id")}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return execute_assignments(self, ctx, items)
