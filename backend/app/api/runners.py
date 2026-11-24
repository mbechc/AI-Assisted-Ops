"""Runners overview: specialist workers, what they run, queue depth, container health and the schedule."""
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import redis
from croniter import croniter
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import dockerapi, workers
from app.config import get_settings
from app.deps import current_user, get_db
from app.framework import registry
from app.models import TaskConfig, TaskRun, WorkerHeartbeat

router = APIRouter(prefix="/runners", tags=["runners"])


def _next_times(cron: str, start: datetime, end: datetime, limit: int = 96) -> list[datetime]:
    if not croniter.is_valid(cron):
        return []
    it = croniter(cron, start)
    out = []
    while len(out) < limit:
        t = it.get_next(datetime)
        if t > end:
            break
        out.append(t)
    return out


@router.get("")
def runners(db: Session = Depends(get_db), _=Depends(current_user)):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    worker_of = {k: d.worker for k, d in registry.load_all().items()}

    try:
        ws = workers.snapshot()
        redis_ok = True
    except Exception:  # noqa: BLE001
        ws = [{"name": n, "label": l, "queue": f"run.{n}", "queue_depth": None, "heartbeat": None, "alive": False,
               "active": [], "counters": {}} for n, l in workers.WORKERS.items()]
        redis_ok = False

    runs = db.scalars(select(TaskRun).where(TaskRun.created_at >= since).order_by(TaskRun.id.desc())).all()
    open_runs = db.scalars(select(TaskRun).where(TaskRun.status.in_(("queued", "running")))).all()
    by_worker_24h: dict[str, Counter] = defaultdict(Counter)
    failures: dict[str, list] = defaultdict(list)
    for r in runs:
        w = worker_of.get(r.task_key, "default")
        by_worker_24h[w][r.status] += 1
        if r.status == "failed" and len(failures[w]) < 5:
            failures[w].append({"run_id": r.id, "task_key": r.task_key, "finished_at": r.finished_at,
                                "error": (r.error or "").strip().splitlines()[-1:] or [""]})
    run_by_id = {r.id: r for r in open_runs}

    containers, docker_error = [], None
    try:
        containers = dockerapi.list_containers()
        stats = dockerapi.container_stats([c["full_id"] for c in containers if c["state"] == "running"])
        for c in containers:
            c["stats"] = stats.get(c["full_id"])
    except dockerapi.DockerUnavailable as exc:
        docker_error = f"container metrics unavailable: {exc}"
    by_service = {c["service"]: c for c in containers}
    persisted = {h.name: h for h in db.scalars(select(WorkerHeartbeat)).all()}

    out_workers = []
    for w in ws:
        active = []
        for a in w["active"]:
            args = a.get("args") or []
            run = run_by_id.get(args[0]) if a.get("task") == "steward.run_task" and args else None
            active.append({"task": a.get("task"), "started": a.get("started"),
                           "run_id": run.id if run else None, "task_key": run.task_key if run else (
                               args[0] if a.get("task") == "steward.execute_items" and args else None)})
        queued = [{"run_id": r.id, "task_key": r.task_key, "created_at": r.created_at}
                  for r in open_runs if r.status == "queued" and worker_of.get(r.task_key) == w["name"]]
        hb = persisted.get(w["name"])
        c = by_service.get(f"worker-{w['name']}")
        out_workers.append({
            **{k: v for k, v in w.items() if k != "active"},
            "active": active, "queued_runs": queued,
            "tasks": sorted(k for k, v in worker_of.items() if v == w["name"]),
            "runs_24h": dict(by_worker_24h.get(w["name"], {})), "failures_24h": failures.get(w["name"], []),
            "restarts_seen": hb.restarts if hb else 0, "last_seen": hb.last_seen if hb else None,
            "container": _container_out(c),
        })

    schedule = []
    cfgs = {c.task_key: c for c in db.scalars(select(TaskConfig)).all()}
    last = {}
    for r in runs:
        last.setdefault(r.task_key, r)
    for key, defn in sorted(registry.REGISTRY.items(), key=lambda kv: kv[1].worker):
        cfg = cfgs.get(key)
        cron = cfg.cron if cfg else defn.default_cron
        enabled = bool(cfg.enabled) if cfg else True
        lr = last.get(key)
        schedule.append({
            "task_key": key, "name": defn.name, "worker": defn.worker, "cron": cron, "enabled": enabled,
            "next": [t.isoformat() for t in _next_times(cron, now, now + timedelta(hours=24))] if enabled else [],
            "last_run": {"id": lr.id, "status": lr.status, "created_at": lr.created_at,
                         "finished_at": lr.finished_at} if lr else None,
        })

    others = [_container_out(c) for c in containers if not c["service"].startswith("worker-")]
    hb = None
    try:
        r = redis.Redis.from_url(get_settings().redis_url, socket_timeout=3)
        hb = r.get("steward:scheduler:heartbeat")
        legacy = {q: r.llen(q) for q in ("default", "execute")}
    except Exception:  # noqa: BLE001
        legacy = {}
    return {
        "now": now, "redis_ok": redis_ok, "docker_error": docker_error,
        "scheduler_alive": bool(hb and time.time() - float(hb) < 180),
        "workers": out_workers, "services": others, "schedule": schedule, "legacy_queues": legacy,
    }


def _container_out(c: dict | None) -> dict | None:
    if not c:
        return None
    s = c.get("stats") or {}
    return {"id": c["id"], "service": c["service"], "name": c["name"], "state": c["state"], "health": c["health"],
            "restarts": c["restarts"], "started_at": c["started_at"], "oom_killed": c["oom_killed"],
            "cpu_percent": s.get("cpu_percent"), "mem_bytes": s.get("mem_bytes"),
            "mem_limit": c.get("mem_limit") or s.get("mem_limit")}
