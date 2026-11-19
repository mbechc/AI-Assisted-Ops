"""Shift summary and owner reports: facts from the database, written up by template or (optionally) by AI.

The template text is always produced and is the source of truth. With AI on, an operator can ask for a short
narrative of the same facts, or ticket-ready wording for an owner report. AI only rewrites the facts it is
given; numbers in the answer are checked against them and the template is shown when the check fails.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.framework import registry
from app.llm.base import LLMProvider, LLMUnavailable
from app.models import TaskRun, WorkItem
from app.store import cache_get, cache_set

OPEN = ("proposed", "reported")
DONE = ("done",)

SHIFT_SYSTEM = """You write the shift handover for network operators of a Cisco Catalyst Center operations
platform. Use only the facts given; do not invent devices, sites or numbers. Lead with what needs attention,
then what was fixed. Plain sentences, at most 120 words, no headings. Return JSON only."""
SHIFT_SCHEMA = {"type": "object", "properties": {"narrative": {"type": "string"}}, "required": ["narrative"]}

OWNER_SYSTEM = """You write a ticket for the team that owns a set of network devices. The platform found that
Cisco Catalyst Center cannot collect data from them, and a resync will not help. Use only the facts given.
Write a short title and a ticket body: what is wrong, the impact, which devices (keep the list as given), and
the steps to fix it. Plain text, no markdown tables. Return JSON only."""
OWNER_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"]}
OWNER_TASK = "telemetry_baseline"
OWNER_MAX_DEVICES = 50


def _aware(ts):
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


def shift_facts(db: Session, hours: int = 12) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    names = {k: d.name for k, d in registry.load_all().items() if d.category != "Collector"}
    per = {k: Counter() for k in names}
    new_groups: dict[str, Counter] = defaultdict(Counter)
    rows = db.execute(select(WorkItem.task_key, WorkItem.status, WorkItem.group_label, WorkItem.created_at,
                             WorkItem.decided_at, WorkItem.decided_by, WorkItem.executed_at,
                             WorkItem.verify_status, WorkItem.outcome)).all()
    for key, status, group, created, decided, decided_by, executed, verify, outcome in rows:
        if key not in per:
            continue
        c = per[key]
        created, decided, executed = _aware(created), _aware(decided), _aware(executed)
        if status in OPEN:
            c["open"] += 1
        if created and created >= since:
            c["new"] += 1
            new_groups[key][group or "-"] += 1
        if executed and executed >= since:
            if status in DONE:
                c["auto_done" if decided_by == "auto" else "done"] += 1
            elif status == "failed":
                c["failed"] += 1
            if verify == "failed":
                c["verify_failed"] += 1
        if decided and decided >= since:
            if status == "rejected":
                c["rejected"] += 1
            elif status == "superseded" and not (outcome or {}).get("removed_from_inventory"):
                c["resolved_outside"] += 1
    failed_runs = [{"task": names.get(r.task_key, r.task_key), "run_id": r.id, "error": (r.error or "")[:200]}
                   for r in db.scalars(select(TaskRun).where(TaskRun.status == "failed", TaskRun.created_at >= since)
                                       .order_by(TaskRun.id.desc()).limit(10))]
    tasks = []
    for key, c in per.items():
        if not any(c.values()):
            continue
        top = [{"group": g, "count": n} for g, n in new_groups[key].most_common(3)]
        tasks.append({"key": key, "name": names[key], **{k: c[k] for k in (
            "open", "new", "done", "auto_done", "failed", "verify_failed", "rejected", "resolved_outside")},
            "top_new_groups": top})
    tasks.sort(key=lambda t: (-(t["failed"] + t["verify_failed"]), -t["new"], -t["open"]))
    total = Counter()
    for t in tasks:
        for k in ("open", "new", "done", "auto_done", "failed", "verify_failed", "rejected", "resolved_outside"):
            total[k] += t[k]
    return {"hours": hours, "since": since.isoformat(), "generated_at": now.isoformat(), "totals": dict(total),
            "tasks": tasks, "failed_runs": failed_runs}


def shift_template(f: dict) -> list[str]:
    t = f["totals"]
    lines = []
    attention = [x for x in f["tasks"] if x["failed"] or x["verify_failed"]]
    if f["failed_runs"]:
        lines.append(f"{len(f['failed_runs'])} task run(s) failed: " + "; ".join(
            f"{r['task']} (run {r['run_id']}): {r['error'] or 'no error text'}" for r in f["failed_runs"][:3]) + ".")
    for x in attention:
        lines.append(f"{x['name']}: {x['failed']} change(s) failed and {x['verify_failed']} failed verification.")
    lines.append(f"{t.get('new', 0)} new item(s) in the last {f['hours']} hours; {t.get('open', 0)} open in total.")
    for x in f["tasks"]:
        if x["new"]:
            top = ", ".join(f"{g['group']} ({g['count']})" for g in x["top_new_groups"])
            lines.append(f"{x['name']}: {x['new']} new" + (f", mostly {top}" if top else "") + ".")
    fixed = t.get("done", 0) + t.get("auto_done", 0)
    if fixed or t.get("resolved_outside"):
        lines.append(f"Fixed: {t.get('done', 0)} approved and executed, {t.get('auto_done', 0)} executed "
                     f"automatically, {t.get('resolved_outside', 0)} resolved directly in Catalyst Center.")
    if t.get("rejected"):
        lines.append(f"{t['rejected']} proposal(s) rejected by reviewers.")
    if len(lines) == 1 and not t.get("open"):
        lines = [f"Quiet shift: nothing new and nothing open in the last {f['hours']} hours."]
    return lines


def _numbers_ok(text: str, facts: dict) -> bool:
    """Every number in the AI text must occur in the facts (guards against invented counts)."""
    allowed = set(re.findall(r"\d+", json.dumps(facts, default=str)))
    return all(n in allowed for n in re.findall(r"\d+", text))


def _sig(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _shift_key(provider: LLMProvider, f: dict) -> str:
    stable = {k: v for k, v in f.items() if k not in ("since", "generated_at")}
    return f"ai:shift:{provider.name}:{_sig(stable)}"


def cached_shift_ai(db: Session, provider: LLMProvider | None, f: dict) -> dict | None:
    return cache_get(db, _shift_key(provider, f)) if provider else None


def shift_ai(db: Session, provider: LLMProvider, f: dict) -> dict:
    cached = cached_shift_ai(db, provider, f)
    if cached:
        return cached
    facts = {k: v for k, v in f.items() if k not in ("since", "generated_at")}
    out = provider.chat_json(SHIFT_SYSTEM, "Facts:\n" + json.dumps(facts, indent=1), SHIFT_SCHEMA)
    text = str((out or {}).get("narrative") or "").strip()[:1500]
    if not text:
        raise LLMUnavailable("empty answer")
    checked = _numbers_ok(text, facts)
    res = {"provider": provider.name, "provider_label": provider.label, "narrative": text if checked else None,
           "rejected_reason": None if checked else "The AI text contained numbers not in the facts; showing the "
                                                    "template instead.",
           "generated_at": datetime.now(timezone.utc).isoformat()}
    cache_set(db, _shift_key(provider, f), res, 3600)
    return res


def owner_groups(db: Session) -> list[dict]:
    """Open telemetry owner-report items grouped by failure cause, with template ticket text."""
    from app.usecases.telemetry import ADVICE, LABELS
    items = db.scalars(select(WorkItem).where(WorkItem.task_key == OWNER_TASK, WorkItem.status.in_(OPEN),
                                              WorkItem.action_type == "report")
                       .order_by(WorkItem.group_label, WorkItem.target_label)).all()
    by: dict[str, list[WorkItem]] = defaultdict(list)
    for i in items:
        by[(i.payload or {}).get("class") or "collection_failed"].append(i)
    out = []
    for cls, its in sorted(by.items(), key=lambda kv: -len(kv[1])):
        devices = [{"hostname": (i.payload or {}).get("hostname") or i.target_label,
                    "ip": i.target_label.rsplit("(", 1)[-1].rstrip(")") if "(" in i.target_label else "",
                    "site": (i.payload or {}).get("site") or "", "platform": (i.payload or {}).get("platform") or "",
                    "error": ((i.payload or {}).get("error_description") or (i.payload or {}).get("collection_status")
                              or "")[:160]} for i in its]
        errors = Counter(d["error"] for d in devices if d["error"])
        sites = Counter(d["site"] for d in devices)
        label = LABELS.get(cls, cls)
        title = f"{label}: Catalyst Center cannot collect from {len(devices)} device(s)"
        listed = devices[:OWNER_MAX_DEVICES]
        body = "\n".join(
            [f"Catalyst Center reports {label.lower()} for {len(devices)} managed, reachable device(s). "
             "Inventory and assurance data for them is missing or out of date; a resync does not fix this.",
             "",
             "Most common errors: " + "; ".join(f"{e} ({n})" for e, n in errors.most_common(3)) if errors else
             "No error text from Catalyst Center.",
             "Sites: " + ", ".join(f"{s or 'unassigned'} ({n})" for s, n in sites.most_common(5)),
             "", "Devices:"]
            + [f"- {d['hostname']} {d['ip']} {d['site']}".rstrip() + (f" - {d['error']}" if d["error"] else "")
               for d in listed]
            + ([f"- ... and {len(devices) - len(listed)} more"] if len(devices) > len(listed) else [])
            + ["", "Action: " + ADVICE.get(cls, "Check the Catalyst Center collection error."),
               "The platform closes these findings automatically once collection works again."])
        out.append({"class": cls, "label": label, "count": len(devices), "devices": listed, "title": title,
                    "body": body, "item_ids": [i.id for i in its]})
    return out


def _owner_key(provider: LLMProvider, g: dict) -> str:
    return f"ai:owner:{provider.name}:{_sig({'c': g['class'], 'd': g['devices'], 'n': g['count']})}"


def cached_owner_ai(db: Session, provider: LLMProvider | None, g: dict) -> dict | None:
    return cache_get(db, _owner_key(provider, g)) if provider else None


def owner_ai(db: Session, provider: LLMProvider, g: dict) -> dict:
    cached = cached_owner_ai(db, provider, g)
    if cached:
        return cached
    facts = {"cause": g["label"], "device_count": g["count"], "devices": g["devices"],
             "fix": g["body"].rsplit("Action: ", 1)[-1]}
    out = provider.chat_json(OWNER_SYSTEM, "Facts:\n" + json.dumps(facts, indent=1), OWNER_SCHEMA, num_ctx=8192)
    title = str((out or {}).get("title") or "").strip()[:200]
    body = str((out or {}).get("body") or "").strip()[:6000]
    if not title or not body:
        raise LLMUnavailable("empty answer")
    missing = [d["hostname"] for d in g["devices"] if d["hostname"] not in body]
    ok = not missing
    res = {"provider": provider.name, "provider_label": provider.label, "title": title if ok else None,
           "body": body if ok else None,
           "rejected_reason": None if ok else f"The AI text left out {len(missing)} device(s); showing the template.",
           "generated_at": datetime.now(timezone.utc).isoformat()}
    cache_set(db, _owner_key(provider, g), res, 86400)
    return res
