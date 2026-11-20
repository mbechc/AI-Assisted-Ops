"""Shift summary (Dashboard) and telemetry owner reports, with optional AI wording."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.audit import audit
from app.deps import current_user, get_db, require
from app.framework import digest
from app.llm.base import LLMUnavailable
from app.llm.factory import effective_mode, get_provider
from app.models import User

router = APIRouter(tags=["digest"])
HOURS = (8, 12, 24)


class ShiftAiIn(BaseModel):
    hours: int = 12


class OwnerAiIn(BaseModel):
    cls: str


def _hours(h: int) -> int:
    if h not in HOURS:
        raise HTTPException(400, f"hours must be one of {HOURS}")
    return h


def _provider_or_400(db: Session):
    p = get_provider(db)
    if p is None:
        raise HTTPException(400, "AI is off. Choose Local LLM or Azure OpenAI under Settings to use AI wording.")
    return p


@router.get("/digest/shift")
def get_shift(hours: int = 12, db: Session = Depends(get_db), _=Depends(current_user)):
    f = digest.shift_facts(db, _hours(hours))
    return {**f, "lines": digest.shift_template(f), "ai_mode": effective_mode(db),
            "ai": digest.cached_shift_ai(db, get_provider(db), f)}


@router.post("/digest/shift/ai")
def shift_ai(body: ShiftAiIn, db: Session = Depends(get_db), user: User = Depends(require("operator"))):
    p = _provider_or_400(db)
    f = digest.shift_facts(db, _hours(body.hours))
    try:
        res = digest.shift_ai(db, p, f)
    except (LLMUnavailable, ValueError) as exc:
        raise HTTPException(502, f"{p.label} unavailable: {exc}")
    audit(db, user.username, "digest.shift_ai", "digest", str(body.hours), provider=p.name)
    db.commit()
    return {**f, "lines": digest.shift_template(f), "ai_mode": effective_mode(db), "ai": res}


@router.get("/digest/owner-reports")
def get_owner_reports(db: Session = Depends(get_db), _=Depends(current_user)):
    p = get_provider(db)
    groups = digest.owner_groups(db)
    return {"ai_mode": effective_mode(db),
            "groups": [{**g, "ai": digest.cached_owner_ai(db, p, g)} for g in groups]}


@router.post("/digest/owner-reports/ai")
def owner_report_ai(body: OwnerAiIn, db: Session = Depends(get_db), user: User = Depends(require("admin"))):
    p = _provider_or_400(db)
    g = next((g for g in digest.owner_groups(db) if g["class"] == body.cls), None)
    if g is None:
        raise HTTPException(404, "no open owner report for this cause")
    try:
        res = digest.owner_ai(db, p, g)
    except (LLMUnavailable, ValueError) as exc:
        raise HTTPException(502, f"{p.label} unavailable: {exc}")
    audit(db, user.username, "digest.owner_report_ai", "task", digest.OWNER_TASK, provider=p.name, cause=body.cls,
          devices=g["count"])
    db.commit()
    return {**g, "ai": res}
