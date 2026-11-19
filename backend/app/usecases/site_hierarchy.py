"""Site hierarchy drift: compare the Catalyst Center hierarchy with an approved baseline (report only).

An operator approves the current hierarchy as the baseline; later runs report sites that were added,
removed, renamed, moved or changed type since, plus hygiene findings (buildings without floors, empty
floors, duplicate names under one parent). Nothing is changed in Catalyst Center.
"""
import difflib
import hashlib
import re
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.framework.base import AnalysisResult, ExecResult, ParamSpec, Proposal, RunContext, TaskDefinition
from app.framework.registry import register
from app.inventory import ensure_fresh
from app.llm.base import LLMUnavailable
from app.models import SiteBaseline, WorkItem
from app.store import cache_get, cache_set
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


_ABBREV = [(re.compile(r"\b(bldg|bld|blg|bygn)\b"), "building"), (re.compile(r"\b(flr|fl|etg)\b"), "floor"),
           (re.compile(r"\blvl\b"), "level"), (re.compile(r"\bhq\b"), "headquarters")]
RENAME_SYSTEM = """You compare Cisco Catalyst Center site names. Each pair is a site that was deleted and a site
that was created under the same parent with the same type. Decide if the new site is the old site under a new
name (abbreviation, spelling, numbering format, language), i.e. a rename done by delete and re-create.
Answer same=false when the names point to different places, different numbers or different floors.
Give a short reason. Return JSON only."""
RENAME_SCHEMA = {
    "type": "object",
    "properties": {"pairs": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "same": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["id", "same", "reason"]}}},
    "required": ["pairs"],
}
RENAME_AI_MAX_PAIRS = 40


def norm_site_name(name: str) -> str:
    s = (name or "").lower().replace("_", " ").replace("-", " ")
    for rx, repl in _ABBREV:
        s = rx.sub(repl, s)
    s = re.sub(r"\d+", lambda m: str(int(m.group())), s)
    return re.sub(r"[^a-z0-9]", "", s)


def _similarity(a: str, b: str) -> float:
    na, nb = norm_site_name(a), norm_site_name(b)
    if not na or not nb:
        return 0.0
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        return 0.0 if re.findall(r"\d+", na) and re.findall(r"\d+", nb) else \
            difflib.SequenceMatcher(None, na, nb).ratio() * 0.7
    return 1.0 if na == nb else difflib.SequenceMatcher(None, na, nb).ratio()


def _ai_confirm(ctx: RunContext, pairs: list[tuple[dict, dict, float]]) -> dict[int, str]:
    """Indexes of pairs the AI says are the same site, with its reason. Empty on any failure."""
    if ctx.llm is None or not pairs:
        return {}
    pairs = pairs[:RENAME_AI_MAX_PAIRS]
    lines = [f"{i}. parent '{a['hierarchy'].rsplit('/', 1)[0]}', type {a['type']}: deleted '{a['name']}' -> "
             f"created '{b['name']}'" for i, (a, b, _) in enumerate(pairs)]
    sig = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    key = f"ai:rename:{ctx.source.id}:{ctx.llm.name}:{sig[:32]}"
    cached = cache_get(ctx.db, key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    try:
        out = ctx.llm.chat_json(RENAME_SYSTEM, "Pairs:\n" + "\n".join(lines), RENAME_SCHEMA)
    except (LLMUnavailable, ValueError, TypeError) as e:
        ctx.log(f"AI rename check unavailable ({e}); ambiguous pairs reported as added and removed")
        return {}
    same = {}
    for r in (out or {}).get("pairs") or []:
        try:
            i = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(pairs) and r.get("same") is True:
            same[i] = str(r.get("reason") or "")[:200]
    cache_set(ctx.db, key, {str(k): v for k, v in same.items()}, 30 * 86400)
    ctx.log(f"{ctx.llm.label} rename check: {len(same)} of {len(pairs)} ambiguous pairs confirmed as renames")
    return same


def match_recreated(ctx: RunContext, removed: list[dict], added: list[dict], p: dict) -> list[dict]:
    """Pair removed and added sites (same parent and type) that are one site renamed by delete and re-create.

    Equal-after-normalising and clearly similar names are paired by rules. Pairs in the grey zone are paired only
    when AI confirms them; without AI they stay separate added/removed findings.
    """
    min_sim = float(p.get("rename_min_similarity", 0.8))
    ai_min = float(p.get("rename_ai_min_similarity", 0.4))
    cands = []
    for a in removed:
        for b in added:
            if a["type"] == b["type"] and a["parent_id"] == b["parent_id"]:
                sim = _similarity(a["name"], b["name"])
                if sim >= ai_min:
                    cands.append((a, b, sim))
    cands.sort(key=lambda c: -c[2])
    used: set[str] = set()
    out = []
    grey = []
    for a, b, sim in cands:
        if a["id"] in used or b["id"] in used:
            continue
        rivals = [c for c in cands if c[2] >= sim - 0.05 and (c[0] is a) != (c[1] is b)
                  and c[0]["id"] not in used and c[1]["id"] not in used]
        if sim >= 1.0 or (sim >= min_sim and not rivals):
            used |= {a["id"], b["id"]}
            out.append({"before": a, "after": b, "similarity": round(sim, 2), "method": "exact" if sim >= 1 else "fuzzy",
                        "confidence": 90 if sim >= 1 else 75})
        else:
            grey.append((a, b, sim))
    grey = [g for g in grey if g[0]["id"] not in used and g[1]["id"] not in used]
    if grey and ctx.llm is None:
        ctx.log(f"Renames: {len(grey)} possible rename(s) need confirmation; AI is off, so they are reported "
                "as added and removed")
    for i, reason in sorted(_ai_confirm(ctx, grey).items(), key=lambda kv: -grey[kv[0]][2]):
        a, b, sim = grey[i]
        if a["id"] in used or b["id"] in used:
            continue
        used |= {a["id"], b["id"]}
        out.append({"before": a, "after": b, "similarity": round(sim, 2), "method": "ai", "confidence": 65,
                    "provider": ctx.llm.label, "reason": reason})
    return out


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
    ai_features = {
        "rename_detection": "Recognises a removed and an added site as one rename (e.g. 'Bldg 2' -> 'Building 02')",
    }
    params = [
        ParamSpec("report_empty_floors", "Report floors without any device", "bool", True),
        ParamSpec("report_buildings_without_floors", "Report buildings without floors", "bool", True),
        ParamSpec("snapshot_max_age_hours", "Max inventory snapshot age (h) before collecting inline", "float", 3),
        ParamSpec("detect_renames", "Report a removed and an added site with similar names as one rename", "bool", True),
        ParamSpec("rename_min_similarity", "Name similarity (0-1) to call a rename without AI", "float", 0.8),
        ParamSpec("rename_ai_min_similarity", "Lowest similarity (0-1) sent to AI for confirmation", "float", 0.4),
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
            added = [cur[sid] for sid in sorted(cur.keys() - old.keys())]
            removed = [old[sid] for sid in sorted(old.keys() - cur.keys())]
            matches = match_recreated(ctx, removed, added, p) if p.get("detect_renames", True) else []
            paired = set()
            for m in matches:
                a, b = m["before"], m["after"]
                paired |= {a["id"], b["id"]}
                stats["recreated"] += 1
                how = {"exact": "the names are the same after normalising",
                       "fuzzy": f"the names are {m['similarity']:.0%} similar",
                       "ai": f"{m['provider']} confirmed: {m['reason']}"}[m["method"]]
                findings.append(self._finding(
                    "recreated", f"{a['id']}>{b['id']}", m["confidence"], b["hierarchy"],
                    f"{a['type'].title()} '{a['name']}' was renamed to '{b['name']}' by deleting and re-creating it "
                    f"under {b['hierarchy'].rsplit('/', 1)[0]} ({how}). The site ID changed, so devices, settings "
                    "and floor maps on the old site were likely not carried over.",
                    {"before": a, "after": b, "method": m["method"], "similarity": m["similarity"]},
                    group="Renamed (re-created)"))
            for s in added:
                if s["id"] in paired:
                    continue
                sid = s["id"]
                stats["added"] += 1
                findings.append(self._finding("added", sid, 100, s["hierarchy"],
                                              f"{s['type'].title()} {s['hierarchy']} was added after the baseline.",
                                              {"after": s}))
            for s in removed:
                if s["id"] in paired:
                    continue
                sid = s["id"]
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
    def _finding(kind: str, ref: str, conf: int, label: str, text: str, payload: dict,
                 group: str | None = None) -> Proposal:
        group = group or kind.replace("_", " ").capitalize()
        return Proposal(
            target_key=f"{kind}:{ref}", target_label=label, action_type="report",
            payload={"kind": kind, "site_id": ref, **payload, "auto_eligible": False},
            rule_confidence=conf,
            evidence=[{"signal": kind, "label": group, "available": True, "weight": 1.0,
                       "detail": text, "buildings": [], "floors": [], "agrees": True}],
            rationale=text, group_label=group,
            fingerprint=f"{kind}:{ref}:{label}",
        )

    def superseded_outcome(self, item: WorkItem, observed: dict) -> dict:
        return {**observed, "matched": None}

    def execute(self, ctx: RunContext, items: list[WorkItem]) -> dict[int, ExecResult]:
        return {i.id: ExecResult(False, "report-only task: nothing to execute") for i in items}
