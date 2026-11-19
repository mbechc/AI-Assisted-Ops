"""Reviewer-feedback insights: what operators reject or override, and what that suggests tuning.

The rule-based part always works: rejection and override rates per group and per evidence signal, the most
recent reviewer notes and plain hints. With AI on, an operator can ask for a summary of the notes into themes
and suggested parameter changes. Suggestions are advice only; nothing is applied automatically.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.framework import registry
from app.llm.base import LLMProvider, LLMUnavailable
from app.models import TaskConfig, WorkItem
from app.store import cache_get, cache_set

WINDOW_DAYS = 90
MIN_GROUP = 5
GROUP_BAD_RATE = 0.30
MIN_SIGNAL = 10
SIGNAL_BAD_RATE = 0.25
MAX_NOTES = 40
NOTE_CHARS = 300

AI_SYSTEM = """You help network operators tune an automation task in a Cisco Catalyst Center operations
platform. You get the task description, its tunable parameters with current values, statistics on what
reviewers rejected or overrode, and recent reviewer notes. Summarise the main reasons for rejections in a few
themes and suggest parameter changes that would reduce wrong proposals. Only suggest parameters from the list,
with a value of the right type. If the notes do not support a change, suggest nothing. Be brief and concrete.
Return JSON only."""
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": {
            "type": "object",
            "properties": {"theme": {"type": "string"}, "count": {"type": "integer"}, "example": {"type": "string"}},
            "required": ["theme", "count", "example"]}},
        "suggestions": {"type": "array", "items": {
            "type": "object",
            "properties": {"param": {"type": "string"}, "value": {"type": "string"}, "why": {"type": "string"}},
            "required": ["param", "value", "why"]}},
    },
    "required": ["summary", "themes", "suggestions"],
}


def _outcome(status: str, override, outcome) -> str | None:
    if status == "rejected":
        return "rejected"
    if status in ("approved", "executing", "done", "failed"):
        return "overridden" if override else "approved"
    if status == "superseded":
        m = (outcome or {}).get("matched")
        return "approved" if m is True else "overridden" if m is False else None
    return None


def insights(db: Session, task_key: str, days: int = WINDOW_DAYS) -> dict:
    defn = registry.get(task_key)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.scalars(select(WorkItem).where(WorkItem.task_key == task_key, WorkItem.decided_at >= since,
                                             WorkItem.decided_by.is_not(None), WorkItem.decided_by != "auto")
                      .order_by(WorkItem.decided_at.desc())).all()
    totals: Counter = Counter()
    groups: dict[str, Counter] = defaultdict(Counter)
    signals: dict[str, Counter] = defaultdict(Counter)
    labels: dict[str, str] = {}
    notes = []
    for i in rows:
        res = _outcome(i.status, i.override_payload, i.outcome)
        if res is None:
            continue
        totals[res] += 1
        groups[i.group_label or "(none)"][res] += 1
        for e in i.evidence or []:
            if e.get("available") and e.get("agrees") and e.get("weight"):
                signals[e["signal"]][res] += 1
                labels[e["signal"]] = e.get("label") or e["signal"]
        if i.decision_note and res != "approved" and len(notes) < MAX_NOTES:
            notes.append({"item_id": i.id, "target": i.target_label, "group": i.group_label, "outcome": res,
                          "note": i.decision_note[:NOTE_CHARS], "decided_by": i.decided_by,
                          "decided_at": i.decided_at})

    def row(name: str, c: Counter, label: str | None = None) -> dict:
        n = sum(c.values())
        bad = c["rejected"] + c["overridden"]
        return {"name": name, "label": label or name, "decided": n, "approved": c["approved"],
                "rejected": c["rejected"], "overridden": c["overridden"], "bad_rate": round(bad / n, 3) if n else 0}

    group_rows = sorted((row(g, c) for g, c in groups.items()), key=lambda r: (-r["bad_rate"], -r["decided"]))
    signal_rows = sorted((row(s, c, labels.get(s)) for s, c in signals.items()), key=lambda r: -r["bad_rate"])
    weight_params = {p.key for p in defn.params if p.key.startswith("weight_")}
    hints = []
    for r in group_rows:
        if r["decided"] >= MIN_GROUP and r["bad_rate"] >= GROUP_BAD_RATE:
            hints.append(f"{r['bad_rate']:.0%} of the {r['decided']} decisions in group '{r['label']}' were rejected "
                         "or overridden: check whether this group needs an exclusion or different parameters.")
    for r in signal_rows:
        if r["decided"] >= MIN_SIGNAL and r["bad_rate"] >= SIGNAL_BAD_RATE:
            p = f"weight_{r['name']}"
            tip = f" Consider lowering '{p}'." if p in weight_params else ""
            hints.append(f"When '{r['label']}' supported a proposal, {r['bad_rate']:.0%} of {r['decided']} were "
                         f"rejected or overridden.{tip}")
    n = sum(totals.values())
    return {"task_key": task_key, "days": days, "decided": n, "approved": totals["approved"],
            "rejected": totals["rejected"], "overridden": totals["overridden"],
            "bad_rate": round((totals["rejected"] + totals["overridden"]) / n, 3) if n else 0,
            "groups": group_rows[:15], "signals": signal_rows, "notes": notes, "hints": hints}


def _ai_key(task_key: str, provider: str) -> str:
    return f"ai:insights:{task_key}:{provider}:{date.today().isoformat()}"


def cached_ai_summary(db: Session, task_key: str, provider: LLMProvider | None) -> dict | None:
    return cache_get(db, _ai_key(task_key, provider.name)) if provider else None


def ai_summary(db: Session, task_key: str, provider: LLMProvider, base: dict) -> dict:
    """Summarise reviewer notes with AI (once per day per task). Raises LLMUnavailable on provider failure."""
    cached = cached_ai_summary(db, task_key, provider)
    if cached:
        return cached
    defn = registry.get(task_key)
    cfg = db.get(TaskConfig, task_key)
    current = {**{p.key: p.default for p in defn.params}, **((cfg.params if cfg else None) or {})}
    specs = {p.key: p for p in defn.params}
    lines = [f"Task: {defn.name}. {defn.description}",
             f"Last {base['days']} days: {base['decided']} reviewed, {base['rejected']} rejected, "
             f"{base['overridden']} overridden.",
             "Parameters (key, type, current value, meaning):"]
    lines += [f"- {k} ({s.type}) = {current.get(k)!r}: {s.label}" for k, s in specs.items()]
    if base["groups"]:
        lines.append("Groups with rejected or overridden items: " + "; ".join(
            f"{g['label']}: {g['rejected']} rejected, {g['overridden']} overridden of {g['decided']}"
            for g in base["groups"] if g["rejected"] or g["overridden"]))
    if base["signals"]:
        lines.append("Evidence signals: " + "; ".join(
            f"{s['label']}: {s['bad_rate']:.0%} bad of {s['decided']}" for s in base["signals"]))
    lines.append("Reviewer notes on rejected or overridden items:")
    lines += [f"- [{n['outcome']}] {n['target']}: {n['note']}" for n in base["notes"]] or ["- (none)"]
    out = provider.chat_json(AI_SYSTEM, "\n".join(lines), AI_SCHEMA, num_ctx=8192)
    if not isinstance(out, dict):
        raise LLMUnavailable("unexpected answer")
    suggestions = []
    for s in out.get("suggestions") or []:
        spec = specs.get(str(s.get("param")))
        if spec is None or spec.type == "json":
            continue
        try:
            value = {"int": int, "float": float, "str": str}.get(spec.type, str)(s.get("value")) \
                if spec.type != "bool" else str(s.get("value")).strip().lower() in ("true", "1", "yes")
        except (TypeError, ValueError):
            continue
        if value == current.get(spec.key):
            continue
        suggestions.append({"param": spec.key, "label": spec.label, "current": current.get(spec.key),
                            "value": value, "why": str(s.get("why") or "")[:300]})
    result = {"provider": provider.name, "provider_label": provider.label,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "summary": str(out.get("summary") or "")[:1200],
              "themes": [{"theme": str(t.get("theme") or "")[:200], "count": int(t.get("count") or 0),
                          "example": str(t.get("example") or "")[:300]} for t in (out.get("themes") or [])[:8]
                         if isinstance(t, dict)],
              "suggestions": suggestions[:8], "notes_used": len(base["notes"])}
    cache_set(db, _ai_key(task_key, provider.name), result, 86400)
    return result
