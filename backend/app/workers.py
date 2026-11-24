"""Specialist worker containers: queue routing, heartbeats and the container healthcheck.

Every TaskDefinition names the worker (domain) it runs on. Each worker container consumes only
`run.<worker>`, so a crash or leak in one domain cannot stall the others.
"""
import json
import os
import socket
import sys
import threading
import time

import redis

from app.config import get_settings

WORKERS = {
    "default": "Shared collector and housekeeping",
    "wireless": "Wireless specialists (APs)",
    "inventory": "Inventory and site hygiene",
    "health": "Device health and telemetry",
}
HEARTBEAT_SECONDS = 15
HEARTBEAT_TTL = 60
KEY = "steward:worker:{name}"
ACTIVE_KEY = "steward:worker:{name}:active"
STATS_KEY = "steward:worker:{name}:stats"


def _redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=5)


def worker_name() -> str:
    return os.environ.get("WORKER_NAME", "default")


def queue_for(task_key: str) -> str:
    from app.framework import registry
    return f"run.{registry.get(task_key).worker}"


def enqueue_run(run_id: int, task_key: str) -> None:
    from app.celery_app import run_task
    run_task.apply_async(args=[run_id], queue=queue_for(task_key))


def enqueue_execute(task_key: str, ids: list[int]) -> None:
    from app.celery_app import execute_items_task
    execute_items_task.apply_async(args=[task_key, ids], queue=queue_for(task_key))


# ------------------------------------------------------------------ heartbeats

def start_heartbeat(queues: list[str], concurrency: int | None) -> None:
    name = worker_name()
    started = time.time()

    def beat() -> None:
        r = _redis()
        n = 0
        while True:
            try:
                r.set(KEY.format(name=name), json.dumps({
                    "name": name, "hostname": socket.gethostname(), "pid": os.getpid(), "queues": queues,
                    "concurrency": concurrency, "started_at": started, "ts": time.time(),
                }), ex=HEARTBEAT_TTL)
            except Exception:  # noqa: BLE001 - redis briefly unavailable; retry next beat
                pass
            if n % 4 == 0:
                try:
                    _persist(name, queues, started)
                except Exception:  # noqa: BLE001
                    pass
            n += 1
            time.sleep(HEARTBEAT_SECONDS)

    threading.Thread(target=beat, name="steward-heartbeat", daemon=True).start()


def _persist(name: str, queues: list[str], started: float) -> None:
    from datetime import datetime, timezone

    from app.db import session_scope
    from app.models import WorkerHeartbeat

    started_at = datetime.fromtimestamp(started, timezone.utc)
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, name)
        if row is None:
            row = WorkerHeartbeat(name=name, restarts=0)
            db.add(row)
        elif row.started_at and abs((row.started_at - started_at).total_seconds()) > 1:
            row.restarts = (row.restarts or 0) + 1
        row.hostname, row.queues, row.started_at = socket.gethostname(), queues, started_at
        row.last_seen = datetime.now(timezone.utc)


def mark_active(task_id: str, task_name: str, args: list) -> None:
    r = _redis()
    key = ACTIVE_KEY.format(name=worker_name())
    r.hset(key, task_id, json.dumps({"task": task_name, "args": list(args or []), "started": time.time()}))
    r.expire(key, 8 * 3600)


def mark_done(task_id: str, failed: bool) -> None:
    r = _redis()
    name = worker_name()
    r.hdel(ACTIVE_KEY.format(name=name), task_id)
    r.hincrby(STATS_KEY.format(name=name), "failed" if failed else "succeeded", 1)


def snapshot() -> list[dict]:
    """State of every known worker as seen in Redis (for the Runners page)."""
    r = _redis()
    out = []
    for name, label in WORKERS.items():
        raw = r.get(KEY.format(name=name))
        hb = json.loads(raw) if raw else None
        active = [{"task_id": k.decode(), **json.loads(v)} for k, v in r.hgetall(ACTIVE_KEY.format(name=name)).items()]
        stats = {k.decode(): int(v) for k, v in r.hgetall(STATS_KEY.format(name=name)).items()}
        out.append({"name": name, "label": label, "queue": f"run.{name}", "queue_depth": r.llen(f"run.{name}"),
                    "heartbeat": hb, "alive": bool(hb and time.time() - hb["ts"] < HEARTBEAT_TTL),
                    "active": active, "counters": stats})
    return out


def check() -> int:
    try:
        raw = _redis().get(KEY.format(name=worker_name()))
        return 0 if raw and time.time() - json.loads(raw)["ts"] < HEARTBEAT_TTL else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
