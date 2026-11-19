"""Review queue filters, shared by the item list, the groups, the facets and bulk decisions.

Values of one attribute are OR-ed ("Catalyst Center is A or B"); different attributes are AND-ed.
"""
from __future__ import annotations

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select

from app.models import WorkItem


class ItemFilter(BaseModel):
    task_key: list[str] = []
    task_key_not: list[str] = []
    status: list[str] = []
    status_not: list[str] = []
    source_id: list[int] = []
    source_id_not: list[int] = []
    group: list[str] = []
    group_not: list[str] = []
    group_contains: str | None = None
    target_contains: str | None = None
    q: str | None = None
    min_confidence: int | None = None
    max_confidence: int | None = None
    blocked: bool | None = None
    include_blocked: bool = False  # bulk decisions only

    def without(self, *fields: str) -> "ItemFilter":
        return self.model_copy(update={f: self.model_fields[f].default for f in fields})


def _split(values: list[str]) -> list[str]:
    """Accept both repeated parameters and the older comma-joined form (status=approved,executing)."""
    return [p for v in values for p in v.split(",") if p]


def item_filter(
    task_key: list[str] = Query([]), task_key_not: list[str] = Query([]),
    status: list[str] = Query([]), status_not: list[str] = Query([]),
    source_id: list[int] = Query([]), source_id_not: list[int] = Query([]),
    group: list[str] = Query([]), group_not: list[str] = Query([]),
    group_contains: str | None = None, target_contains: str | None = None, q: str | None = None,
    min_confidence: int | None = None, max_confidence: int | None = None, blocked: bool | None = None,
) -> ItemFilter:
    return ItemFilter(task_key=task_key, task_key_not=task_key_not, status=_split(status),
                      status_not=_split(status_not), source_id=source_id, source_id_not=source_id_not,
                      group=group, group_not=group_not, group_contains=group_contains or None,
                      target_contains=target_contains or None, q=q or None, min_confidence=min_confidence,
                      max_confidence=max_confidence, blocked=blocked)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def apply(stmt: Select, f: ItemFilter) -> Select:
    w = WorkItem
    if f.task_key:
        stmt = stmt.where(w.task_key.in_(f.task_key))
    if f.task_key_not:
        stmt = stmt.where(w.task_key.not_in(f.task_key_not))
    if f.status:
        stmt = stmt.where(w.status.in_(f.status))
    if f.status_not:
        stmt = stmt.where(w.status.not_in(f.status_not))
    if f.source_id:
        stmt = stmt.where(w.source_id.in_(f.source_id))
    if f.source_id_not:
        stmt = stmt.where(w.source_id.is_(None) | w.source_id.not_in(f.source_id_not))
    if f.group:
        stmt = stmt.where(w.group_label.in_(f.group))
    if f.group_not:
        stmt = stmt.where(w.group_label.is_(None) | w.group_label.not_in(f.group_not))
    if f.group_contains:
        stmt = stmt.where(w.group_label.ilike(_like(f.group_contains), escape="\\"))
    if f.target_contains:
        stmt = stmt.where(w.target_label.ilike(_like(f.target_contains), escape="\\"))
    if f.q:
        stmt = stmt.where(w.target_label.ilike(_like(f.q), escape="\\") | w.group_label.ilike(_like(f.q), escape="\\"))
    if f.min_confidence is not None:
        stmt = stmt.where(w.confidence >= f.min_confidence)
    if f.max_confidence is not None:
        stmt = stmt.where(w.confidence <= f.max_confidence)
    if f.blocked is True:
        stmt = stmt.where(w.blocked_reason.is_not(None))
    elif f.blocked is False:
        stmt = stmt.where(w.blocked_reason.is_(None))
    return stmt
