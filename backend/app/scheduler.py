"""Database-driven scheduler: cron expressions live in task_configs and are editable in the UI."""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
from croniter import croniter
from sqlalchemy import select, update

from app.config import get_settings
from app.db import session_scope
from app.framework import registry
from app.framework.runner import RunAlreadyActive, create_run, dispatch_approved, ensure_task_configs
from app.logging_setup import configure_logging
from app.models import TaskConfig, TaskRun

log = logging.getLogger("scheduler")
HEARTBEAT = Path("/tmp/scheduler.heartbeat")
TICK_SECONDS = 30
STALE_RUN_HOURS = 8


def tick() -> None:
    from app.workers import enqueue_run

    now = datetime.now(timezone.utc)
    to_start: list[tuple[int, str]] = []
    with session_scope() as db:
        ensure_task_configs(db)
        for cfg in db.scalars(select(TaskConfig).where(TaskConfig.enabled.is_(True))).all():
            if cfg.task_key not in registry.REGISTRY or not croniter.is_valid(cfg.cron):
                continue
            base = cfg.last_scheduled_at or cfg.updated_at or now
            next_due = croniter(cfg.cron, base).get_next(datetime)
            if next_due <= now:
                cfg.last_scheduled_at = now
                try:
                    run = create_run(db, cfg.task_key, "schedule", "scheduler")
                    to_start.append((run.id, cfg.task_key))
                    log.info("scheduled run %s for %s", run.id, cfg.task_key)
                except RunAlreadyActive as exc:
                    log.info("skip %s: %s", cfg.task_key, exc)
        # Runs orphaned by a worker restart are marked failed so the task can run again.
        cutoff = datetime.fromtimestamp(now.timestamp() - STALE_RUN_HOURS * 3600, timezone.utc)
        db.execute(update(TaskRun).where(TaskRun.status.in_(("queued", "running")), TaskRun.created_at < cutoff)
                   .values(status="failed", error="run did not finish (worker restart or timeout)",
                           finished_at=now))
    for run_id, task_key in to_start:
        enqueue_run(run_id, task_key)
    dispatch_approved()


def main() -> None:
    configure_logging()
    registry.load_all()
    log.info("scheduler started with tasks: %s", ", ".join(registry.REGISTRY))
    while True:
        try:
            tick()
            HEARTBEAT.write_text(str(time.time()))
            redis.Redis.from_url(get_settings().redis_url).set("aiops:scheduler:heartbeat", str(time.time()), ex=600)
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        time.sleep(TICK_SECONDS)


def check() -> int:
    try:
        return 0 if time.time() - float(HEARTBEAT.read_text()) < 5 * TICK_SECONDS else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check())
    main()
