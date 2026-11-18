"""Small key/value helpers for global settings and cached collector data."""
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import CacheEntry, Setting

DEFAULT_SETTINGS: dict[str, Any] = {
    "kill_switch": False,
    "global_autonomy_cap": 1,
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return row.value.get("v")


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value={"v": value}))
        db.flush()
    else:
        row.value = {"v": value}


def all_settings(db: Session) -> dict[str, Any]:
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


def cache_get(db: Session, key: str) -> Any | None:
    row = db.get(CacheEntry, key)
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    return row.value if expires >= datetime.now(timezone.utc) else None


def cache_set(db: Session, key: str, value: Any, ttl_seconds: int) -> None:
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    row = db.get(CacheEntry, key)
    if row is None:
        db.add(CacheEntry(key=key, value=value, expires_at=expires))
    else:
        row.value = value
        row.expires_at = expires
