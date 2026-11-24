import time

import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import user_out
from app.audit import audit
from app.config import get_settings
from app.connectors.base import CONNECTOR_KINDS
from app.deps import current_user, get_db, require
from app.framework.runner import revoke_open_auto_items
from app.llm.factory import effective_mode, get_provider
from app.api.review import SAVED_KEY as SAVED_FILTERS_KEY
from app.models import AuditLog, Setting, User
from app.security import ROLES, hash_password
from app.store import all_settings, get_setting, set_setting
from app.workers import WORKERS

router = APIRouter(tags=["admin"])


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


ANNOUNCEMENT_MAX = 500


class SettingsIn(BaseModel):
    kill_switch: bool | None = None
    global_autonomy_cap: int | None = None
    announcement: str | None = None


@router.get("/users")
def list_users(db: Session = Depends(get_db), _=Depends(require("admin"))):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.username)).all()]


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    if body.role not in ROLES:
        raise HTTPException(400, "invalid role")
    if len(body.password) < 10:
        raise HTTPException(400, "password must be at least 10 characters")
    if db.scalars(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, "user exists")
    u = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    db.add(u)
    audit(db, admin.username, "user.created", "user", body.username, role=body.role)
    db.commit()
    return user_out(u)


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "not found")
    changes = {}
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, "invalid role")
        if body.role != "admin" and _last_admin(db, u):
            raise HTTPException(400, "the last active admin cannot lose the admin role")
        u.role, changes["role"] = body.role, body.role
    if body.is_active is not None:
        if u.id == admin.id and not body.is_active:
            raise HTTPException(400, "you cannot deactivate yourself")
        if not body.is_active and _last_admin(db, u):
            raise HTTPException(400, "the last active admin cannot be deactivated")
        u.is_active, changes["is_active"] = body.is_active, body.is_active
    if body.password:
        if len(body.password) < 10:
            raise HTTPException(400, "password must be at least 10 characters")
        u.password_hash, changes["password"] = hash_password(body.password), "reset"
    audit(db, admin.username, "user.updated", "user", u.username, **changes)
    db.commit()
    return user_out(u)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    """Decisions and the audit log keep the username as text, so the history stays readable."""
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404, "not found")
    if u.id == admin.id:
        raise HTTPException(400, "you cannot delete yourself")
    if _last_admin(db, u):
        raise HTTPException(400, "the last active admin cannot be deleted")
    db.get(Setting, SAVED_FILTERS_KEY, with_for_update=True)
    filters = get_setting(db, SAVED_FILTERS_KEY) or []
    kept = [f for f in filters if f.get("owner") != u.username or f.get("shared")]
    if len(kept) != len(filters):
        set_setting(db, SAVED_FILTERS_KEY, kept)
    db.delete(u)
    audit(db, admin.username, "user.deleted", "user", u.username, role=u.role)
    db.commit()
    return {"deleted": u.username}


def _last_admin(db: Session, u: User) -> bool:
    if u.role != "admin" or not u.is_active:
        return False
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) <= 1


@router.get("/settings")
def get_settings_api(db: Session = Depends(get_db), _=Depends(current_user)):
    return all_settings(db)


@router.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db), admin: User = Depends(require("admin"))):
    before = all_settings(db)
    if body.global_autonomy_cap is not None:
        if not 0 <= body.global_autonomy_cap <= 3:
            raise HTTPException(400, "cap must be 0-3")
        set_setting(db, "global_autonomy_cap", body.global_autonomy_cap)
    if body.kill_switch is not None:
        set_setting(db, "kill_switch", body.kill_switch)
    if body.announcement is not None:
        body.announcement = body.announcement.strip()
        if len(body.announcement) > ANNOUNCEMENT_MAX:
            raise HTTPException(400, f"the message can be at most {ANNOUNCEMENT_MAX} characters")
        set_setting(db, "announcement", body.announcement)
    after = {**before, **body.model_dump(exclude_none=True)}
    if after["kill_switch"] or after["global_autonomy_cap"] < before["global_autonomy_cap"]:
        revoke_open_auto_items(db)
    audit(db, admin.username, "settings.updated", "settings", None, before=before, after=after)
    db.commit()
    return all_settings(db)


@router.get("/audit")
def list_audit(limit: int = 100, offset: int = 0, action: str | None = None, q: str | None = None,
               db: Session = Depends(get_db), _=Depends(require("admin"))):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if q:
        stmt = stmt.where((AuditLog.actor.ilike(f"%{q}%")) | (AuditLog.object_id.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).limit(min(limit, 500)).offset(offset)).all()
    return {"total": total, "items": [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
                                       "object_type": r.object_type, "object_id": r.object_id,
                                       "details": r.details} for r in rows]}


@router.get("/connector-kinds")
def connector_kinds(_=Depends(current_user)):
    return [k.__dict__ for k in CONNECTOR_KINDS.values()]


@router.get("/system/status")
def system_status(db: Session = Depends(get_db), _=Depends(current_user)):
    s = get_settings()
    r = redis.Redis.from_url(s.redis_url)
    hb = r.get("steward:scheduler:heartbeat")
    workers = {}
    try:
        from app.celery_app import celery
        workers = celery.control.inspect(timeout=1.5).ping() or {}
    except Exception:  # noqa: BLE001
        pass
    provider = get_provider(db)
    return {
        "version": s.steward_version,
        "ai_mode": effective_mode(db),
        "llm": provider.status() if provider else {"provider": None, "ready": False},
        "scheduler_alive": bool(hb and time.time() - float(hb) < 180),
        "workers": sorted(workers.keys()),
        "queue_depth": {f"run.{w}": r.llen(f"run.{w}") for w in WORKERS},
        "kill_switch": bool(get_setting(db, "kill_switch")),
    }
