"""Helpers shared by the specialist tasks."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.framework.base import ExecResult, RunContext
from app.models import WorkItem


def open_targets(ctx: RunContext, task_key: str) -> set[str]:
    """Targets with an open work item for this task on this run's source."""
    q = select(WorkItem.target_key).where(WorkItem.task_key == task_key,
                                          WorkItem.status.in_(("proposed", "reported")))
    q = q.where(WorkItem.source_id == ctx.source.id) if ctx.source else q.where(WorkItem.source_id.is_(None))
    return set(ctx.db.scalars(q).all())


def epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def fail_all(items: list[WorkItem], detail: str, **data) -> dict[int, ExecResult]:
    return {i.id: ExecResult(False, detail, data=data or None) for i in items}


def chunks(seq: list, size: int):
    size = max(1, size)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
