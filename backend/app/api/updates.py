from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import updates
from app.audit import audit
from app.deps import current_user, get_db, require
from app.models import User
from app.security import ROLE_RANK

router = APIRouter(tags=["updates"])


class ChannelIn(BaseModel):
    channel: Literal["production", "development"]


class InstallIn(BaseModel):
    version: str


def _state(user: User) -> dict:
    return {**updates.state(), "can_manage": ROLE_RANK.get(user.role, -1) >= ROLE_RANK["admin"]}


def _queue(action: str, arg: str | None = None) -> None:
    try:
        updates.queue(action, arg)
    except OSError as exc:
        raise HTTPException(503, f"cannot reach the host updater folder: {exc}") from exc


@router.get("/updates")
def get_updates(user: User = Depends(current_user)):
    return _state(user)


@router.post("/updates/refresh")
def refresh(user: User = Depends(require("admin"))):
    _queue("check")
    return _state(user)


@router.put("/updates/channel")
def set_channel(body: ChannelIn, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    st = updates.state()
    if st["install_running"]:
        raise HTTPException(409, "an update is in progress")
    _queue("channel", body.channel)
    audit(db, user.username, "update.channel", "appliance", None, previous=st["channel"], channel=body.channel)
    db.commit()
    return _state(user)


@router.post("/updates/install")
def install(body: InstallIn, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    st = updates.state()
    if st["install_running"]:
        raise HTTPException(409, "an update is already in progress")
    if st["connectivity"]["status"] != "connected":
        raise HTTPException(409, "the host cannot reach GitHub; press Refresh and check the endpoints")
    if not st["available"] or st["available"]["version"] != body.version:
        raise HTTPException(409, f"{body.version} is not the update offered on the {st['channel']} track")
    _queue("install", body.version)
    audit(db, user.username, "update.install", "appliance", None, channel=st["channel"],
          from_version=st["current_version"], to_version=body.version)
    db.commit()
    return _state(user)


@router.get("/updates/log")
def update_log(_=Depends(require("admin"))):
    return {"log": updates.log_tail()}


@router.get("/system/connectivity")
def system_connectivity(_=Depends(current_user)):
    return updates.connectivity()
