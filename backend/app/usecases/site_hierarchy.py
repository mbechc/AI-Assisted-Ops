"""Site hierarchy drift: compare the Catalyst Center hierarchy with an approved baseline (report only).

An operator approves the current hierarchy as the baseline; later runs report sites that were added,
removed, renamed, moved or changed type since, plus hygiene findings (buildings without floors, empty
floors, duplicate names under one parent). Nothing is changed in Catalyst Center.
"""
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh
from app.models import SiteBaseline, WorkItem
from app.usecases.common import open_targets


def active_baseline(db: Session, source_id: int) -> SiteBaseline | None:
    return db.scalars(select(SiteBaseline).where(SiteBaseline.source_id == source_id, SiteBaseline.active.is_(True))
                      .order_by(SiteBaseline.id.desc())).first()


def approve_baseline(db: Session, source_id: int, sites: list[dict], actor: str) -> SiteBaseline:
    for b in db.scalars(select(SiteBaseline).where(SiteBaseline.source_id == source_id)).all():
        b.active = False
    b = SiteBaseline(source_id=source_id, approved_by=actor, sites=sites, active=True)
    db.add(b)
    db.flush()
    return b


@register
class SiteHierarchyDrift(TaskDefinition):
    key = "site_hierarchy_drift"
    name = "Site hierarchy drift"
    description = ("Compares the Catalyst Center site hierarchy with the approved baseline and reports sites that "
                   "were added, removed, renamed, moved or changed type, plus hygiene findings (buildings without "
                   "floors, empty floors, duplicate names). Report only: approve a new baseline when the changes "
                   "are intended.")
    category = "Inventory hygiene"
    worker = "inventory"
    default_cron = "10 6 * * *"
    max_autonomy = 0
    default_autonomy_level = 0
    default_autonomy_cap = 0
    action_types = {"report": "Report only (no change in Catalyst Center)"}
    params = [
        ParamSpec("report_empty_floors", "Report floors without any device", "bool", True),
        ParamSpec("report_buildings_without_floors", "Report buildings without floors", "bool", True),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
    ]

    def analyze(self, ctx: RunContext) -> AnalysisResult:
        p = ctx.params
        inv = ensure_fresh(ctx, float(p.get("snapshot_max_age_hours", 3)))
        cur = {s.id: s.to_dict() for s in inv.sites}
        base = active_baseline(ctx.db, ctx.source.id)
        findings: list[Proposal] = []
        stats = Counter()

        if base is None:
            findings.append(self._finding("baseline", "none", 50, "No approved baseline",
                                          f"No approved baseline yet. Review the current hierarchy ({len(cur)} sites) "
                                          "and approve it as the baseline on the task page.", {"sites": len(cur)}))
        else:
            old = {s["id"]: s for s in base.sites}
            for sid in cur.keys() - old.keys():
                s = cur[sid]
                stats["added"] += 1
                findings.append(self._finding("added", sid, 100, s["hierarchy"],
                                              f"{s['type'].title()} {s['hierarchy']} was added after the baseline.",
                                              {"after": s}))
            for sid in old.keys() - cur.keys():
                s = old[sid]
                stats["removed"] += 1
                findings.append(self._finding("removed", sid, 100, s["hierarchy"],
                                              f"{s['type'].title()} {s['hierarchy']} was removed.", {"before": s}))
            for sid in cur.keys() & old.keys():
                a, b = old[sid], cur[sid]
                if a["name"] != b["name"]:
                    stats["renamed"] += 1
                    findings.append(self._finding("renamed", sid, 100, b["hierarchy"],
                                                  f"Renamed from '{a['name']}' to '{b['name']}'.",
                                                  {"before": a, "after": b}))
                if a["parent_id"] != b["parent_id"]:
                    stats["moved"] += 1
                    pa = a["hierarchy"].rsplit("/", 1)[0]
                    pb = b["hierarchy"].rsplit("/", 1)[0]
                    findings.append(self._finding("moved", sid, 100, b["hierarchy"],
                                                  f"Moved from {pa} to {pb}.", {"before": a, "after": b}))
                if a["type"] != b["type"]:
                    stats["type_changed"] += 1
                    findings.append(self._finding("type_changed", sid, 100, b["hierarchy"],
                                                  f"Type changed from {a['type']} to {b['type']}.",
                                                  {"before": a, "after": b}))

        children: dict[str, list[dict]] = defaultdict(list)
        for s in cur.values():
            if s["parent_id"]:
                children[s["parent_id"]].append(s)
        used = Counter(inv.assignments.values())
        for s in cur.values():
            kids = children.get(s["id"], [])
            if s["type"] == "building" and p.get("report_buildings_without_floors", True) \
                    and not any(k["type"] == "floor" for k in kids):
                stats["building_without_floors"] += 1
                findings.append(self._finding("building_without_floors", s["id"], 90, s["hierarchy"],
                                              f"Building {s['hierarchy']} has no floors; APs cannot be placed.",
                                              {"site": s, "devices": used.get(s["id"], 0)}))
            if s["type"] == "floor" and p.get("report_empty_floors", True) and not used.get(s["id"]):
                stats["empty_floor"] += 1
                findings.append(self._finding("empty_floor", s["id"], 70, s["hierarchy"],
                                              f"Floor {s['hierarchy']} has no devices.", {"site": s}))
            names = Counter(k["name"].lower() for k in kids)
            for name, n in names.items():
                if n > 1:
                    stats["duplicate_name"] += 1
                    findings.append(self._finding("duplicate_name", f"{s['id']}:{name}", 90, s["hierarchy"],
                                                  f"{n} sites named '{name}' under {s['hierarchy']}.",
                                                  {"parent": s, "name": name}))

        keys = {f.target_key for f in findings}
        resolved = {t: {"no_longer_present": True} for t in open_targets(ctx, self.key) if t not in keys}
        ctx.log(f"Hierarchy: {len(cur)} sites; baseline "
                f"{'from ' + base.approved_at.strftime('%Y-%m-%d') + ' by ' + base.approved_by if base else 'missing'}; "
                + ", ".join(f"{k} {v}" for k, v in stats.items()))
        return AnalysisResult(proposals=findings, resolved=resolved, stats={
            "sites": len(cur), "baseline": bool(base), "baseline_sites": len(base.sites) if base else 0,
            "findings": len(findings), **stats})

    @staticmethod
    def _finding(kind: str, ref: str, conf: int, label: str, text: str, payload: dict) -> Proposal:
        return Proposal(
            target_key=f"{kind}:{ref}", target_label=label, action_type="report",
            payload={"kind": kind, "site_id": ref, **payload, "auto_eligible": False},
            rule_confidence=conf,
            evidence=[{"signal": kind, "label": kind.replace("_", " ").capitalize(), "available": True, "weight": 1.0,
                       "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=text, group_label=kind.replace("_", " ").capitalize(),
            fingerprint=f"{kind}:{ref}:{label}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return {i.id: ExecResult(False, "report-only task: nothing to execute") for i in items}
