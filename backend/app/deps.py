from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import User
from app.security import ROLE_RANK, decode_session_token

SESSION_COOKIE = "aiops_session"


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    auth = request.headers.get("Authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:]
    claims = decode_session_token(token) if token else None
    if not claims:
        raise HTTPException(401, "not authenticated")
    user = db.scalars(select(User).where(User.username == claims["sub"])).first()
    if user is None or not user.is_active:
        raise HTTPException(401, "not authenticated")
    return user


def require(role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[role]:
            raise HTTPException(403, f"requires role {role}")
        return user
    return dep
