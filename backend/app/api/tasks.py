from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.celery_app import dispatch_task
from app.workers import enqueue_run
from app.deps import current_user, get_db, require
from app.framework import registry
from app.framework.autonomy import effective_autonomy
from app.framework.base import LEVEL_NAMES
from app.framework.calibration import calibration
from app.framework.insights import ai_summary, cached_ai_summary, insights
from app.framework.runner import RunAlreadyActive, create_run, decide, ensure_task_configs
from app.item_filter import ItemFilter, apply, item_filter
from app.llm.base import LLMUnavailable
from app.llm.factory import effective_mode, get_provider
from app.models import DataSource, TaskConfig, TaskRun, User, WorkItem

router = APIRouter(tags=["tasks"])


class TaskConfigIn(BaseModel):
    enabled: bool | None = None
    cron: str | None = None
    source_id: int | None = None
    clear_source: bool = False
    autonomy_level: int | None = None
    autonomy_cap: int | None = None
    auto_threshold: int | None = None
    max_auto_per_run: int | None = None
    params: dict | None = None


class DecisionIn(BaseModel):
    decision: str
    ids: list[int] | None = None
    filter: ItemFilter | None = None
    note: str | None = None
    override: dict | None = None


def run_out(r: TaskRun, with_log: bool = False) -> dict:
    d = {"id": r.id, "task_key": r.task_key, "source_id": r.source_id, "trigger": r.trigger,
         "triggered_by": r.triggered_by, "status": r.status, "started_at": r.started_at,
         "finished_at": r.finished_at, "created_at": r.created_at, "stats": r.stats, "error": r.error}
    if with_log:
        d["log"] = r.log
    return d


def item_out(i: WorkItem, full: bool = False) -> dict:
    d = {"id": i.id, "task_key": i.task_key, "target_key": i.target_key, "target_label": i.target_label,
         "group_label": i.group_label, "action_type": i.action_type, "confidence": i.confidence,
         "rule_confidence": i.rule_confidence, "llm_confidence": i.llm_confidence, "llm_status": i.llm_status,
         "llm_provider": i.llm_provider, "status": i.status, "blocked_reason": i.blocked_reason, "rationale": i.rationale,
         "concerns": i.concerns, "decided_by": i.decided_by, "decided_at": i.decided_at,
         "verify_status": i.verify_status, "updated_at": i.updated_at, "created_at": i.created_at,
         "source_id": i.source_id, "proposed": _proposed_summary(i)}
    if full:
        d.update({"payload": i.payload, "override_payload": i.override_payload, "evidence": i.evidence,
                  "decision_note": i.decision_note, "exec_result": i.exec_result, "outcome": i.outcome,
                  "executed_at": i.executed_at, "first_run_id": i.first_run_id, "last_run_id": i.last_run_id})
    return d


def _proposed_summary(i: WorkItem) -> str:
    p = {**(i.payload or {}), **(i.override_payload or {})}
    a = i.action_type
    if a == "reassign_site":
        return f"{p.get('current_site') or '?'} -> {p.get('site_hierarchy') or p.get('building') or '?'}"
    if a == "rename_ap":
        return f"{p.get('hostname')} -> {p.get('hostname_new')}"
    if a == "set_role":
        return f"{p.get('current_role')} -> {p.get('role')}"
    if a == "exclude_device":
        return f"Exclude {p.get('days')} days: " + "; ".join(p.get("reasons") or [])
    if a == "include_device":
        return f"End exclusion ({p.get('mechanism')})"
    if a == "resync_device":
        return "Resync inventory"
    if a == "report":
        return (i.rationale or "").split(". ")[0][:160]
    return p.get("site_hierarchy") or p.get("summary") or ""


def task_out(db: Session, key: str, ai_mode: str | None = None) -> dict:
    defn = registry.get(key)
    cfg = db.get(TaskConfig, key)
    counts = dict(db.execute(select(WorkItem.status, func.count()).where(WorkItem.task_key == key)
                             .group_by(WorkItem.status)).all())
    last = db.scalars(select(TaskRun).where(TaskRun.task_key == key).order_by(TaskRun.id.desc())).first()
    next_run = None
    if cfg.enabled and croniter.is_valid(cfg.cron):
        base = cfg.last_scheduled_at or cfg.updated_at or datetime.now(timezone.utc)
        next_run = croniter(cfg.cron, base).get_next(datetime)
    return {
        "key": key, "name": defn.name, "description": defn.description, "category": defn.category,
        "source_kind": defn.source_kind, "action_types": defn.action_types, "worker": defn.worker,
        "optional_sources": defn.optional_sources,
        "ai_features": defn.ai_features, "ai_mode": ai_mode or effective_mode(db),
        "max_autonomy": defn.max_autonomy, "level_names": LEVEL_NAMES,
        "param_specs": [p.__dict__ for p in defn.params],
        "config": {"enabled": cfg.enabled, "cron": cfg.cron, "source_id": cfg.source_id,
                   "autonomy_level": cfg.autonomy_level, "autonomy_cap": cfg.autonomy_cap,
                   "auto_threshold": cfg.auto_threshold, "max_auto_per_run": cfg.max_auto_per_run,
                   "params": cfg.params, "updated_at": cfg.updated_at, "updated_by": cfg.updated_by},
        "autonomy": effective_autonomy(db, defn, cfg).to_dict(),
        "counts": counts, "last_run": run_out(last) if last else None, "next_run": next_run,
    }


@router.get("/tasks")
def list_tasks(db: Session = Depends(get_db), _=Depends(current_user)):
    ensure_task_configs(db)
    db.commit()
    mode = effective_mode(db)
    return [task_out(db, k, mode) for k in sorted(registry.load_all())]


@router.get("/tasks/{key}")
def get_task(key: str, db: Session = Depends(get_db), _=Depends(current_user)):
    try:
        return task_out(db, key)
    except KeyError:
        raise HTTPException(404, "unknown task")


@router.put("/tasks/{key}")
def update_task(key: str, body: TaskConfigIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    try:
        defn = registry.get(key)
    except KeyError:
        raise HTTPException(404, "unknown task")
    cfg = db.get(TaskConfig, key)
    before = {"autonomy_level": cfg.autonomy_level, "autonomy_cap": cfg.autonomy_cap,
              "auto_threshold": cfg.auto_threshold, "enabled": cfg.enabled, "cron": cfg.cron}
    if body.autonomy_cap is not None:
        if not 0 <= body.autonomy_cap <= defn.max_autonomy:
            raise HTTPException(400, f"cap must be between 0 and {defn.max_autonomy} for this task")
        cfg.autonomy_cap = body.autonomy_cap
    if body.autonomy_level is not None:
        if not 0 <= body.autonomy_level <= 3:
            raise HTTPException(400, "level must be 0-3")
        if body.autonomy_level > cfg.autonomy_cap:
            raise HTTPException(400, f"level L{body.autonomy_level} exceeds the task cap L{cfg.autonomy_cap}")
        cfg.autonomy_level = body.autonomy_level
    elif cfg.autonomy_level > cfg.autonomy_cap:
        cfg.autonomy_level = cfg.autonomy_cap
    if body.auto_threshold is not None:
        if not 50 <= body.auto_threshold <= 100:
            raise HTTPException(400, "threshold must be 50-100")
        cfg.auto_threshold = body.auto_threshold
    if body.max_auto_per_run is not None:
        cfg.max_auto_per_run = max(0, body.max_auto_per_run)
    if body.cron is not None and body.cron != cfg.cron:
        if not croniter.is_valid(body.cron):
            raise HTTPException(400, "invalid cron expression")
        cfg.cron = body.cron
        cfg.last_scheduled_at = datetime.now(timezone.utc)
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.clear_source:
        cfg.source_id = None
    elif body.source_id is not None:
        if db.get(DataSource, body.source_id) is None:
            raise HTTPException(400, "unknown source")
        cfg.source_id = body.source_id
    if body.params is not None:
        specs = {p.key: p for p in defn.params}
        clean = dict(cfg.params or {})
        for k, v in body.params.items():
            spec = specs.get(k)
            if spec is None:
                continue
            try:
                clean[k] = {"int": int, "float": float, "bool": bool, "str": str}.get(spec.type, lambda x: x)(v)
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {k}")
        cfg.params = clean
    cfg.updated_by = admin.username
    after = {"autonomy_level": cfg.autonomy_level, "autonomy_cap": cfg.autonomy_cap,
             "auto_threshold": cfg.auto_threshold, "enabled": cfg.enabled, "cron": cfg.cron}
    db.flush()
    if effective_autonomy(db, defn, cfg).level < 2:
        revoked = 0
        for i in db.scalars(select(WorkItem).where(WorkItem.task_key == key, WorkItem.status == "approved",
                                                   WorkItem.decided_by == "auto")).all():
            i.status, i.decided_by, i.decided_at = "proposed", None, None
            i.decision_note = "auto-approval revoked: autonomy level or cap lowered"
            revoked += 1
        after["auto_items_revoked"] = revoked
    action = "task.autonomy_changed" if (before["autonomy_level"], before["autonomy_cap"]) != \
        (after["autonomy_level"], after["autonomy_cap"]) else "task.config_updated"
    audit(db, admin.username, action, "task", key, before=before, after=after,
          params_changed=sorted((body.params or {}).keys()))
    db.commit()
    return task_out(db, key)


@router.post("/tasks/{key}/run")
def trigger_run(key: str, db: Session = Depends(get_db), user: User = Depends(require("operator"))):
    try:
        run = create_run(db, key, "manual", user.username)
    except KeyError:
        raise HTTPException(404, "unknown task")
    except RunAlreadyActive as exc:
        raise HTTPException(409, str(exc))
    audit(db, user.username, "task.run_requested", "task", key, run_id=run.id)
    db.commit()
    enqueue_run(run.id, key)
    return run_out(run)


@router.get("/tasks/{key}/calibration")
def task_calibration(key: str, db: Session = Depends(get_db), _=Depends(current_user)):
    try:
        return calibration(db, key)
    except KeyError:
        raise HTTPException(404, "unknown task")


@router.get("/tasks/{key}/insights")
def task_insights(key: str, db: Session = Depends(get_db), _=Depends(current_user)):
    try:
        base = insights(db, key)
    except KeyError:
        raise HTTPException(404, "unknown task")
    provider = get_provider(db)
    return {**base, "ai_mode": effective_mode(db), "ai": cached_ai_summary(db, key, provider)}


@router.post("/tasks/{key}/insights/ai")
def task_insights_ai(key: str, db: Session = Depends(get_db), user: User = Depends(require("operator"))):
    try:
        base = insights(db, key)
    except KeyError:
        raise HTTPException(404, "unknown task")
    provider = get_provider(db)
    if provider is None:
        raise HTTPException(400, "AI is off. Choose Local LLM or Azure OpenAI under Settings to use AI summaries.")
    if not base["notes"] and not base["decided"]:
        raise HTTPException(400, "No reviewed items yet: nothing to summarise.")
    try:
        result = ai_summary(db, key, provider, base)
    except (LLMUnavailable, ValueError) as exc:
        raise HTTPException(502, f"{provider.label} unavailable: {exc}")
    audit(db, user.username, "task.insights_ai", "task", key, provider=provider.name,
          notes_used=result["notes_used"])
    db.commit()
    return {**base, "ai_mode": effective_mode(db), "ai": result}


@router.get("/runs")
def list_runs(task_key: str | None = None, limit: int = 50, offset: int = 0,
              db: Session = Depends(get_db), _=Depends(current_user)):
    stmt = select(TaskRun)
    if task_key:
        stmt = stmt.where(TaskRun.task_key == task_key)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(TaskRun.id.desc()).limit(min(limit, 200)).offset(offset)).all()
    return {"total": total, "items": [run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), _=Depends(current_user)):
    r = db.get(TaskRun, run_id)
    if r is None:
        raise HTTPException(404, "not found")
    return run_out(r, with_log=True)


@router.get("/items")
def list_items(f: ItemFilter = Depends(item_filter), sort: str = "-confidence", limit: int = 100, offset: int = 0,
               db: Session = Depends(get_db), _=Depends(current_user)):
    stmt = apply(select(WorkItem), f)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = {"confidence": WorkItem.confidence, "target": WorkItem.target_label,
           "updated": WorkItem.updated_at, "group": WorkItem.group_label}.get(sort.lstrip("-"), WorkItem.confidence)
    stmt = stmt.order_by(col.desc() if sort.startswith("-") else col.asc(), WorkItem.id)
    rows = db.scalars(stmt.limit(min(limit, 500)).offset(offset)).all()
    return {"total": total, "items": [item_out(i) for i in rows]}


@router.get("/items/groups")
def item_groups(f: ItemFilter = Depends(item_filter), db: Session = Depends(get_db), _=Depends(current_user)):
    """Items per task, Catalyst Center and group; 'above threshold' uses each task's own auto threshold."""
    thresholds = {c.task_key: c.auto_threshold for c in db.scalars(select(TaskConfig))}
    threshold = case(*[(WorkItem.task_key == k, v) for k, v in thresholds.items()], else_=95) if thresholds else 95
    stmt = apply(select(
        WorkItem.task_key, WorkItem.source_id, WorkItem.group_label, func.count(), func.avg(WorkItem.confidence),
        func.min(WorkItem.confidence), func.max(WorkItem.confidence),
        func.sum(case((WorkItem.blocked_reason.is_(None) & (WorkItem.confidence >= threshold), 1), else_=0)),
        func.sum(case((WorkItem.blocked_reason.is_not(None), 1), else_=0))), f)
    rows = db.execute(stmt.group_by(WorkItem.task_key, WorkItem.source_id, WorkItem.group_label)
                      .order_by(func.count().desc())).all()
    return {"thresholds": thresholds, "groups": [
        {"task_key": t, "source_id": s, "group": g, "count": c, "avg_confidence": round(float(a or 0), 1),
         "min_confidence": mn, "max_confidence": mx, "above_threshold": int(at or 0), "blocked": int(bl or 0),
         "threshold": thresholds.get(t, 95)}
        for t, s, g, c, a, mn, mx, at, bl in rows]}


@router.get("/items/summary")
def items_summary(db: Session = Depends(get_db), _=Depends(current_user)):
    rows = db.execute(select(WorkItem.task_key, WorkItem.status, func.count())
                      .group_by(WorkItem.task_key, WorkItem.status)).all()
    out: dict[str, dict] = {}
    for key, status, n in rows:
        out.setdefault(key, {})[status] = n
    return out


@router.get("/items/kpis")
def items_kpis(db: Session = Depends(get_db), _=Depends(current_user)):
    """Per review category: open items, and what was fixed and what came in during the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    by_status = defaultdict(Counter)
    for key, status, n in db.execute(select(WorkItem.task_key, WorkItem.status, func.count())
                                     .group_by(WorkItem.task_key, WorkItem.status)).all():
        by_status[key][status] = n
    new = dict(db.execute(select(WorkItem.task_key, func.count()).where(WorkItem.created_at >= cutoff)
                          .group_by(WorkItem.task_key)).all())
    done = dict(db.execute(select(WorkItem.task_key, func.count()).where(
        WorkItem.status == "done", WorkItem.executed_at >= cutoff).group_by(WorkItem.task_key)).all())
    resolved: Counter = Counter()
    for key, outcome in db.execute(select(WorkItem.task_key, WorkItem.outcome).where(
            WorkItem.status == "superseded", WorkItem.decided_at >= cutoff)).all():
        if not (outcome or {}).get("removed_from_inventory") and not (outcome or {}).get("source_deleted"):
            resolved[key] += 1
    history = _open_history(db, KPI_DAYS, KPI_STEP_HOURS)
    per_day = 24 // KPI_STEP_HOURS
    out = []
    for key, defn in registry.load_all().items():
        if defn.category == "Collector":
            continue
        s = by_status.get(key, Counter())
        series = history.get(key) or [0] * (KPI_DAYS * per_day + 1)
        series[-1] = s["proposed"] + s["reported"]
        out.append({"key": key, "name": defn.name, "category": defn.category,
                    "proposed": s["proposed"], "reported": s["reported"], "open": series[-1],
                    "approved": s["approved"] + s["executing"], "new_24h": new.get(key, 0),
                    "executed_24h": done.get(key, 0), "resolved_24h": resolved.get(key, 0),
                    "fixed_24h": done.get(key, 0) + resolved.get(key, 0),
                    "open_24h_ago": series[-1 - per_day], "change_24h": series[-1] - series[-1 - per_day],
                    "history": series, "history_step_hours": KPI_STEP_HOURS})
    return out


KPI_DAYS = 30
KPI_STEP_HOURS = 6


def _open_history(db: Session, days: int, step_hours: int) -> dict[str, list[int]]:
    """Open review items per task at fixed intervals: an item is open from creation until it is decided."""
    now = datetime.now(timezone.utc)
    steps = days * 24 // step_hours
    times = [now - timedelta(hours=step_hours * (steps - i)) for i in range(steps + 1)]
    start = times[0]
    events: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    base: Counter = Counter()
    rows = db.execute(select(WorkItem.task_key, WorkItem.created_at, WorkItem.decided_at)
                      .where((WorkItem.decided_at.is_(None)) | (WorkItem.decided_at >= start))).all()
    for key, created, decided in rows:
        created, decided = _aware(created), _aware(decided)
        if created is None or created > now:
            continue
        if created <= start:
            base[key] += 1
        else:
            events[key].append((created, 1))
        if decided is not None and decided >= created:
            events[key].append((decided, -1))
    out = {}
    for key in set(base) | set(events):
        ev = sorted(events[key])
        n, j, series = base[key], 0, []
        for t in times:
            while j < len(ev) and ev[j][0] <= t:
                n += ev[j][1]
                j += 1
            series.append(max(0, n))
        out[key] = series
    return out


def _aware(ts: datetime | None) -> datetime | None:
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


@router.get("/items/{item_id}")
def get_item(item_id: int, db: Session = Depends(get_db), _=Depends(current_user)):
    i = db.get(WorkItem, item_id)
    if i is None:
        raise HTTPException(404, "not found")
    return item_out(i, full=True)


@router.post("/items/decide")
def decide_items(body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(require("operator"))):
    if body.ids:
        ids = body.ids
    elif body.filter:
        f = body.filter.model_copy(update={"status": ["proposed"], "status_not": []})
        if not f.include_blocked:
            f.blocked = False
        ids = list(db.scalars(apply(select(WorkItem.id), f).limit(5000)).all())
    else:
        raise HTTPException(400, "ids or filter required")
    if body.override and len(ids) != 1:
        raise HTTPException(400, "overrides apply to a single item")
    try:
        result = decide(db, ids, body.decision, user.username, body.note, body.override)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.commit()
    if body.decision == "approve" and result["updated"]:
        dispatch_task.delay()
    return result
