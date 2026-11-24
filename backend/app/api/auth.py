from datetime import datetime, timezone
from typing import Literal

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import get_settings
from app.deps import SESSION_COOKIE, current_user, get_db
from app.models import User
from app.security import create_session_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
MAX_FAILURES = 10
LOCKOUT_SECONDS = 900


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class PreferencesIn(BaseModel):
    theme: Literal["dark", "light"]


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active,
            "last_login": u.last_login, "created_at": u.created_at, "theme": u.theme or "dark"}


def _redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    key = f"steward:loginfail:{ip}"
    r = _redis()
    if int(r.get(key) or 0) >= MAX_FAILURES:
        raise HTTPException(429, "too many failed logins, try again later")
    user = db.scalars(select(User).where(User.username == body.username)).first()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        r.incr(key)
        r.expire(key, LOCKOUT_SECONDS)
        audit(db, body.username[:64], "auth.login_failed", "user", body.username[:64], ip=ip)
        db.commit()
        raise HTTPException(401, "invalid username or password")
    r.delete(key)
    user.last_login = datetime.now(timezone.utc)
    audit(db, user.username, "auth.login", "user", user.username, ip=ip)
    db.commit()
    token = create_session_token(user.username, user.role)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=True, samesite="strict",
                        max_age=get_settings().session_hours * 3600, path="/")
    return user_out(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.put("/me/preferences")
def set_preferences(body: PreferencesIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.theme = body.theme
    db.add(user)
    db.commit()
    return user_out(user)


@router.post("/password")
def change_password(body: PasswordIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "current password is wrong")
    if len(body.new_password) < 10:
        raise HTTPException(400, "password must be at least 10 characters")
    user.password_hash = hash_password(body.new_password)
    db.add(user)
    audit(db, user.username, "user.password_changed", "user", user.username)
    db.commit()
    return {"ok": True}
