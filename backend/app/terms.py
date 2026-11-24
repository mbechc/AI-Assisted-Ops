"""Acceptance of Steward's terms (DISCLAIMER.md), recorded by the installer in data/terms/acceptance.json."""
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import get_settings
from app.models import AuditLog

log = logging.getLogger(__name__)


def acceptance() -> dict | None:
    try:
        with open(get_settings().terms_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def audit_acceptance(db: Session) -> None:
    """One audit entry per recorded acceptance, so it shows in the Audit log."""
    rec = acceptance()
    if not rec:
        log.warning("Steward's terms are not accepted on this installation: run scripts/accept-terms.sh")
        return
    ref = f"v{rec.get('terms_version')}:{rec.get('accepted_at')}"[:128]
    if db.scalar(select(AuditLog.id).where(AuditLog.action == "terms.accepted", AuditLog.object_id == ref)):
        return
    audit(db, str(rec.get("accepted_by") or "installer")[:64], "terms.accepted", "terms", ref, **rec)
