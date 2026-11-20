"""Small key/value helpers for global settings and cached collector data."""
import copy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from app.models import CacheEntry, Setting

DEFAULT_SETTINGS: dict[str, Any] = {
    "kill_switch": False,
    "global_autonomy_cap": 1,
    "ai_mode": "none",
}
# Stored like DEFAULT_SETTINGS but not part of GET /settings (secrets live here, encrypted).
PRIVATE_DEFAULTS: dict[str, Any] = {
    "ai_cloud": {"endpoint": "", "deployment": "", "api_version": "2024-10-21", "auth": "api_key",
                 "tenant_id": "", "client_id": "", "api_key_enc": None, "client_secret_enc": None},
}


def get_setting(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS.get(key, PRIVATE_DEFAULTS.get(key)))
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
    """Insert or replace in one statement: tasks on other workers may write the same key concurrently."""
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"unsupported database {dialect}")
    stmt = insert(CacheEntry).values(key=key, value=value, expires_at=expires)
    db.execute(stmt.on_conflict_do_update(index_elements=[CacheEntry.key],
                                          set_={"value": stmt.excluded.value, "expires_at": stmt.excluded.expires_at}))
    cached = db.identity_map.get(identity_key(CacheEntry, key))
    if cached is not None:
        db.expire(cached)
