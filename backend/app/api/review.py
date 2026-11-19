"""Review queue helpers: value counts for the filter builder, and saved filters."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import current_user, get_db
from app.item_filter import ItemFilter, apply, item_filter
from app.models import DataSource, Setting, User, WorkItem
from app.store import get_setting, set_setting

router = APIRouter(prefix="/review", tags=["review"])

SAVED_KEY = "review_saved_filters"
MAX_GROUPS = 500


def _counts(db: Session, col, f: ItemFilter, limit: int | None = None) -> list[tuple]:
    stmt = apply(select(col, func.count()), f).group_by(col).order_by(func.count().desc())
    return db.execute(stmt.limit(limit) if limit else stmt).all()


@router.get("/facets")
def facets(f: ItemFilter = Depends(item_filter), db: Session = Depends(get_db), _=Depends(current_user)):
    """Counts per value of each attribute, with every other active filter applied (not the attribute's own)."""
    by_source = dict(_counts(db, WorkItem.source_id, f.without("source_id", "source_id_not")))
    sources = [{"id": s.id, "name": s.name, "count": by_source.pop(s.id, 0)}
               for s in db.scalars(select(DataSource).order_by(DataSource.name))]
    if by_source.get(None):
        sources.append({"id": None, "name": "(deleted data source)", "count": by_source[None]})
    return {
        "sources": sources,
        "tasks": [{"value": k, "count": n} for k, n in _counts(db, WorkItem.task_key, f.without("task_key", "task_key_not"))],
        "statuses": [{"value": k, "count": n} for k, n in _counts(db, WorkItem.status, f.without("status", "status_not"))],
        "groups": [{"value": k, "count": n} for k, n in _counts(
            db, WorkItem.group_label, f.without("group", "group_not", "group_contains"), MAX_GROUPS) if k is not None],
    }


class SavedFilterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    query: str = Field(max_length=4000)
    shared: bool = False


def _visible(entry: dict, user: User) -> bool:
    return entry["owner"] == user.username or entry.get("shared", False)


def _out(entry: dict, user: User) -> dict:
    return {**entry, "mine": entry["owner"] == user.username,
            "can_delete": entry["owner"] == user.username or user.role == "admin"}


@router.get("/saved-filters")
def list_saved(db: Session = Depends(get_db), user: User = Depends(current_user)):
    entries = [e for e in get_setting(db, SAVED_KEY) or [] if _visible(e, user)]
    return sorted((_out(e, user) for e in entries), key=lambda e: (not e["mine"], e["name"].lower()))


@router.post("/saved-filters")
def save_filter(body: SavedFilterIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.get(Setting, SAVED_KEY, with_for_update=True)
    entries = list(get_setting(db, SAVED_KEY) or [])
    name = body.name.strip()
    existing = next((e for e in entries if e["owner"] == user.username and e["name"].lower() == name.lower()), None)
    entry = {"id": existing["id"] if existing else secrets.token_hex(6), "name": name, "query": body.query,
             "owner": user.username, "shared": body.shared, "saved_at": datetime.now(timezone.utc).isoformat()}
    entries = [e for e in entries if e is not existing] + [entry]
    set_setting(db, SAVED_KEY, entries)
    db.commit()
    return _out(entry, user)


@router.delete("/saved-filters/{filter_id}")
def delete_filter(filter_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    db.get(Setting, SAVED_KEY, with_for_update=True)
    entries = list(get_setting(db, SAVED_KEY) or [])
    entry = next((e for e in entries if e["id"] == filter_id and _visible(e, user)), None)
    if entry is None:
        raise HTTPException(404, "saved filter not found")
    if entry["owner"] != user.username and user.role != "admin":
        raise HTTPException(403, "only the owner or an admin can delete a shared filter")
    set_setting(db, SAVED_KEY, [e for e in entries if e["id"] != filter_id])
    db.commit()
    return {"ok": True}
